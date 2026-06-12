from __future__ import annotations

import base64
import json
import os
import posixpath
import shlex
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .models import RepoFile, ServerConfig, ServerProbe


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str


def run_ssh(
    target: str,
    remote_command: str,
    ssh_options: list[str],
    timeout: int | None = None,
) -> CommandResult:
    cmd = ["ssh", *ssh_options, target, remote_command]
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ssh command failed on {target} with exit {proc.returncode}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return CommandResult(stdout=proc.stdout, stderr=proc.stderr)


def probe_df(config: ServerConfig, job_id: str, repo_id: str, ssh_options: list[str]) -> tuple[str, str, int]:
    best: tuple[str, str, int] | None = None
    safe_repo = repo_id.replace("/", "_")
    for root in config.temp_roots:
        quoted_root = shlex.quote(root)
        command = f"mkdir -p {quoted_root} && df -Pk {quoted_root} | tail -n 1"
        try:
            result = run_ssh(config.ssh_target, command, ssh_options, timeout=30)
        except RuntimeError:
            continue
        parts = result.stdout.strip().split()
        if len(parts) < 4:
            continue
        try:
            free_bytes = int(parts[3]) * 1024
        except ValueError:
            continue
        temp_dir = posixpath.join(root, "msdl", job_id, safe_repo)
        if best is None or free_bytes > best[2]:
            best = (root, temp_dir, free_bytes)
    if best is None:
        raise RuntimeError(f"no usable temp root found for server {config.name}")
    run_ssh(config.ssh_target, f"mkdir -p {shlex.quote(best[1])}", ssh_options, timeout=30)
    return best


def probe_speed(
    config: ServerConfig,
    repo_id: str,
    revision: str,
    sample_path: str,
    bytes_to_read: int,
    ssh_options: list[str],
    forwarded_token: str | None,
) -> float:
    url = (
        f"https://huggingface.co/{quote(repo_id, safe='/')}"
        f"/resolve/{quote(revision, safe='')}/{quote(sample_path, safe='/')}"
    )
    script = r"""
import json
import os
import sys
import time
import urllib.request

url = os.environ["MSDL_PROBE_URL"]
limit = int(os.environ["MSDL_PROBE_BYTES"])
token = (
    os.environ.get("MSDL_HF_TOKEN")
    or os.environ.get("HF_TOKEN")
    or os.environ.get("HUGGINGFACE_HUB_TOKEN")
)

request = urllib.request.Request(url)
request.add_header("Range", f"bytes=0-{limit - 1}")
if token:
    request.add_header("Authorization", f"Bearer {token}")

start = time.monotonic()
read = 0
with urllib.request.urlopen(request, timeout=45) as response:
    while read < limit:
        chunk = response.read(min(1024 * 1024, limit - read))
        if not chunk:
            break
        read += len(chunk)
elapsed = max(time.monotonic() - start, 0.001)
print(json.dumps({"bytes": read, "seconds": elapsed, "bps": read / elapsed}))
"""
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    env = {
        "MSDL_PROBE_URL": url,
        "MSDL_PROBE_BYTES": str(bytes_to_read),
    }
    if forwarded_token:
        env["MSDL_HF_TOKEN"] = forwarded_token
    prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    command = f"{prefix} python3 -c 'import base64; exec(base64.b64decode({encoded!r}).decode())'"
    result = run_ssh(config.ssh_target, command, ssh_options, timeout=90)
    payload = json.loads(result.stdout.strip())
    bps = float(payload["bps"])
    if bps <= 0:
        raise RuntimeError(f"invalid speed probe for {config.name}: {payload}")
    return bps


def download_remote_file(
    config: ServerConfig,
    repo_id: str,
    revision: str,
    file: RepoFile,
    temp_dir: str,
    ssh_options: list[str],
    forwarded_token: str | None,
) -> str:
    remote_path = posixpath.join(temp_dir, file.path)
    command = _download_command(repo_id, revision, file.path, temp_dir, forwarded_token)
    run_ssh(config.ssh_target, command, ssh_options, timeout=None)
    return remote_path


def _download_command(
    repo_id: str,
    revision: str,
    file_path: str,
    temp_dir: str,
    forwarded_token: str | None,
) -> str:
    token_prefix = ""
    if forwarded_token:
        token_prefix = f"export HF_TOKEN={shlex.quote(forwarded_token)}; "

    return textwrap.dedent(
        f"""
        set -e
        mkdir -p {shlex.quote(temp_dir)}
        {token_prefix}
        if python3 -c 'import hf_transfer' >/dev/null 2>&1; then
          export HF_HUB_ENABLE_HF_TRANSFER=1
        fi
        if command -v hf >/dev/null 2>&1; then
          hf download {shlex.quote(repo_id)} {shlex.quote(file_path)} --revision {shlex.quote(revision)} --local-dir {shlex.quote(temp_dir)}
        elif command -v huggingface-cli >/dev/null 2>&1; then
          huggingface-cli download {shlex.quote(repo_id)} {shlex.quote(file_path)} --revision {shlex.quote(revision)} --local-dir {shlex.quote(temp_dir)}
        else
          echo "missing hf or huggingface-cli on worker" >&2
          exit 127
        fi
        test -f {shlex.quote(posixpath.join(temp_dir, file_path))}
        """
    ).strip()


def pull_file_rsync(
    ssh_target: str,
    remote_path: str,
    local_part: Path,
    ssh_options: list[str],
) -> None:
    local_part.parent.mkdir(parents=True, exist_ok=True)
    ssh_command = "ssh"
    if ssh_options:
        ssh_command = " ".join(["ssh", *(shlex.quote(option) for option in ssh_options)])
    cmd = [
        "rsync",
        "-a",
        "-s",
        "--partial",
        "--append-verify",
        "--whole-file",
        "--no-compress",
        "-e",
        ssh_command,
        f"{ssh_target}:{remote_path}",
        str(local_part),
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"rsync failed from {ssh_target}:{remote_path}: {proc.stderr.strip()}")


def pull_file_scp(
    ssh_target: str,
    remote_path: str,
    local_part: Path,
    ssh_options: list[str],
) -> None:
    local_part.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "scp",
        *ssh_options,
        "-p",
        f"{ssh_target}:{remote_path}",
        str(local_part),
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"scp failed from {ssh_target}:{remote_path}: {proc.stderr.strip()}")


def resolve_transfer_backend(requested: str) -> str:
    if requested != "auto":
        return requested
    if os.name == "nt":
        if shutil.which("scp"):
            return "scp"
        if shutil.which("rsync"):
            return "rsync"
        raise RuntimeError("neither scp nor rsync is available on the controller")
    if shutil.which("rsync"):
        return "rsync"
    if shutil.which("scp"):
        return "scp"
    raise RuntimeError("neither rsync nor scp is available on the controller")


def remove_remote_file(config: ServerConfig, remote_path: str, ssh_options: list[str]) -> None:
    run_ssh(config.ssh_target, f"rm -f {shlex.quote(remote_path)}", ssh_options, timeout=30)

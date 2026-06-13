from __future__ import annotations

import base64
import importlib.util
import json
import os
import posixpath
import shlex
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import quote

from .hf import validate_repo_id
from .models import RepoFile, ServerConfig, ServerProbe


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str


_PYTHON_HF_DOWNLOAD_SCRIPT = r"""
import os
import shutil
from pathlib import Path, PurePosixPath

if os.environ.get("MSDL_INSECURE_SKIP_TLS_VERIFY") == "1":
    import httpx
    from huggingface_hub.utils import set_client_factory
    from huggingface_hub.utils._http import hf_request_event_hook

    set_client_factory(
        lambda: httpx.Client(
            event_hooks={"request": [hf_request_event_hook]},
            follow_redirects=True,
            timeout=None,
            verify=False,
        )
    )

from huggingface_hub import hf_hub_download

repo_id = os.environ["MSDL_REPO_ID"]
revision = os.environ["MSDL_REVISION"]
file_path = os.environ["MSDL_FILE_PATH"]
local_dir = os.environ["MSDL_LOCAL_DIR"]
token = (
    os.environ.get("MSDL_HF_TOKEN")
    or os.environ.get("HF_TOKEN")
    or os.environ.get("HUGGINGFACE_HUB_TOKEN")
)

downloaded = Path(
    hf_hub_download(
        repo_id=repo_id,
        filename=file_path,
        revision=revision,
        local_dir=local_dir,
        token=token,
    )
)
expected = Path(local_dir).joinpath(*PurePosixPath(file_path).parts)
if downloaded != expected and downloaded.is_file() and not expected.is_file():
    expected.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(downloaded, expected)
if not expected.is_file():
    raise RuntimeError(f"download did not produce expected file: {expected}")
"""


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
    if config.local:
        return probe_df_local(config, job_id, repo_id)
    if config.platform == "windows":
        return probe_df_windows(config, job_id, repo_id, ssh_options)
    return probe_df_linux(config, job_id, repo_id, ssh_options)


def probe_df_local(config: ServerConfig, job_id: str, repo_id: str) -> tuple[str, str, int]:
    best: tuple[str, str, int] | None = None
    safe_repo = repo_id.replace("/", "_")
    for root in config.temp_roots:
        root_path = Path(root).expanduser()
        try:
            root_path.mkdir(parents=True, exist_ok=True)
            free_bytes = shutil.disk_usage(root_path).free
        except OSError:
            continue
        temp_dir = root_path / "msdl" / job_id / safe_repo
        if best is None or free_bytes > best[2]:
            best = (str(root_path), str(temp_dir), free_bytes)
    if best is None:
        raise RuntimeError(f"no usable temp root found for server {config.name}")
    Path(best[1]).mkdir(parents=True, exist_ok=True)
    return best


def probe_df_linux(config: ServerConfig, job_id: str, repo_id: str, ssh_options: list[str]) -> tuple[str, str, int]:
    best: tuple[str, str, int] | None = None
    safe_repo = repo_id.replace("/", "_")
    for root in config.temp_roots:
        quoted_root = shlex.quote(root)
        command = f"mkdir -p {quoted_root} && df -Pk {quoted_root} | tail -n 1"
        try:
            result = run_ssh(require_ssh_target(config), command, ssh_options, timeout=30)
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
    run_ssh(require_ssh_target(config), f"mkdir -p {shlex.quote(best[1])}", ssh_options, timeout=30)
    return best


def probe_df_windows(config: ServerConfig, job_id: str, repo_id: str, ssh_options: list[str]) -> tuple[str, str, int]:
    best: tuple[str, str, int] | None = None
    safe_repo = repo_id.replace("/", "_")
    for root in config.temp_roots:
        script = textwrap.dedent(
            f"""
            $ErrorActionPreference = 'Stop'
            $root = {ps_quote(root)}
            New-Item -ItemType Directory -Force -LiteralPath $root | Out-Null
            $item = Get-Item -LiteralPath $root
            $drive = Get-PSDrive -Name $item.PSDrive.Name
            [Console]::Out.WriteLine([Int64]$drive.Free)
            """
        )
        try:
            result = run_windows_powershell(require_ssh_target(config), script, ssh_options, timeout=30)
        except RuntimeError:
            continue
        try:
            free_bytes = int(result.stdout.strip())
        except ValueError:
            continue
        temp_dir = windows_path_join(root, "msdl", job_id, safe_repo)
        if best is None or free_bytes > best[2]:
            best = (root, temp_dir, free_bytes)
    if best is None:
        raise RuntimeError(f"no usable temp root found for server {config.name}")
    run_windows_powershell(
        require_ssh_target(config),
        f"New-Item -ItemType Directory -Force -LiteralPath {ps_quote(best[1])} | Out-Null",
        ssh_options,
        timeout=30,
    )
    return best


def probe_speed(
    config: ServerConfig,
    repo_id: str,
    revision: str,
    sample_path: str,
    bytes_to_read: int,
    ssh_options: list[str],
    forwarded_token: str | None,
    insecure_skip_tls_verify: bool,
) -> float:
    url = (
        f"https://huggingface.co/{quote(repo_id, safe='/')}"
        f"/resolve/{quote(revision, safe='')}/{quote(sample_path, safe='/')}"
    )
    script = r"""
import json
import os
import ssl
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
context = None
if os.environ.get("MSDL_INSECURE_SKIP_TLS_VERIFY") == "1":
    context = ssl._create_unverified_context()

start = time.monotonic()
read = 0
with urllib.request.urlopen(request, timeout=45, context=context) as response:
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
    if insecure_skip_tls_verify:
        env["MSDL_INSECURE_SKIP_TLS_VERIFY"] = "1"
    if config.local:
        result = run_local_python_script(encoded, env, timeout=90)
    else:
        command = python_script_command(config, encoded, env)
        result = run_ssh(require_ssh_target(config), command, ssh_options, timeout=90)
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
    insecure_skip_tls_verify: bool,
) -> str:
    if config.local:
        return download_local_file(
            repo_id=repo_id,
            revision=revision,
            file=file,
            temp_dir=temp_dir,
            forwarded_token=forwarded_token,
            insecure_skip_tls_verify=insecure_skip_tls_verify,
        )
    remote_path = remote_path_for_repo_file(config, temp_dir, file.path)
    command = _download_command(
        config,
        repo_id,
        revision,
        file.path,
        temp_dir,
        forwarded_token,
        insecure_skip_tls_verify,
    )
    run_ssh(require_ssh_target(config), command, ssh_options, timeout=None)
    return remote_path


def download_local_file(
    repo_id: str,
    revision: str,
    file: RepoFile,
    temp_dir: str,
    forwarded_token: str | None,
    insecure_skip_tls_verify: bool,
) -> str:
    if insecure_skip_tls_verify:
        return download_local_file_python(
            repo_id=repo_id,
            revision=revision,
            file=file,
            temp_dir=temp_dir,
            forwarded_token=forwarded_token,
            insecure_skip_tls_verify=insecure_skip_tls_verify,
        )

    temp_path = Path(temp_dir).expanduser()
    temp_path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    if forwarded_token:
        env["HF_TOKEN"] = forwarded_token
    if importlib.util.find_spec("hf_transfer"):
        env["HF_HUB_ENABLE_HF_TRANSFER"] = "1"

    hf = shutil.which("hf") or shutil.which("huggingface-cli")
    if not hf:
        raise RuntimeError("missing hf or huggingface-cli on local worker")
    cmd = [
        hf,
        "download",
        repo_id,
        file.path,
        "--revision",
        revision,
        "--local-dir",
        str(temp_path),
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True, env=env, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())

    local_path = temp_path.joinpath(*PurePosixPath(file.path).parts)
    if not local_path.is_file():
        raise RuntimeError(f"download did not produce expected file: {local_path}")
    return str(local_path)


def download_local_file_python(
    repo_id: str,
    revision: str,
    file: RepoFile,
    temp_dir: str,
    forwarded_token: str | None,
    insecure_skip_tls_verify: bool,
) -> str:
    temp_path = Path(temp_dir).expanduser()
    temp_path.mkdir(parents=True, exist_ok=True)
    encoded = base64.b64encode(_PYTHON_HF_DOWNLOAD_SCRIPT.encode("utf-8")).decode("ascii")
    env = download_python_env(repo_id, revision, file.path, str(temp_path), forwarded_token, insecure_skip_tls_verify)
    run_local_python_script(encoded, env, timeout=None)
    local_path = temp_path.joinpath(*PurePosixPath(file.path).parts)
    if not local_path.is_file():
        raise RuntimeError(f"download did not produce expected file: {local_path}")
    return str(local_path)


def check_worker_tools(config: ServerConfig, ssh_options: list[str]) -> None:
    if config.local:
        if shutil.which("hf") or shutil.which("huggingface-cli"):
            return
        raise RuntimeError(f"missing hf or huggingface-cli on worker {config.name}")

    if config.platform == "windows":
        script = textwrap.dedent(
            """
            $ErrorActionPreference = 'Stop'
            if (-not (Get-Command hf -ErrorAction SilentlyContinue) -and
                -not (Get-Command huggingface-cli -ErrorAction SilentlyContinue)) {
              throw 'missing hf or huggingface-cli on worker'
            }
            """
        )
        run_windows_powershell(require_ssh_target(config), script, ssh_options, timeout=30)
        return

    command = (
        "command -v hf >/dev/null 2>&1 || "
        "command -v huggingface-cli >/dev/null 2>&1 || "
        "{ echo 'missing hf or huggingface-cli on worker' >&2; exit 127; }"
    )
    run_ssh(require_ssh_target(config), command, ssh_options, timeout=30)


def write_worker_probe_file(
    config: ServerConfig,
    remote_path: str,
    content: str,
    ssh_options: list[str],
) -> None:
    if config.local:
        path = Path(remote_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="ascii")
        return

    if config.platform == "windows":
        parent = windows_path_parent(remote_path)
        script = textwrap.dedent(
            f"""
            $ErrorActionPreference = 'Stop'
            New-Item -ItemType Directory -Force -LiteralPath {ps_quote(parent)} | Out-Null
            [System.IO.File]::WriteAllText(
              {ps_quote(remote_path)},
              {ps_quote(content)},
              [System.Text.Encoding]::ASCII
            )
            """
        )
        run_windows_powershell(require_ssh_target(config), script, ssh_options, timeout=30)
        return

    parent = posixpath.dirname(remote_path)
    command = (
        f"mkdir -p {shlex.quote(parent)} && "
        f"printf %s {shlex.quote(content)} > {shlex.quote(remote_path)}"
    )
    run_ssh(require_ssh_target(config), command, ssh_options, timeout=30)


def _download_command(
    config: ServerConfig,
    repo_id: str,
    revision: str,
    file_path: str,
    temp_dir: str,
    forwarded_token: str | None,
    insecure_skip_tls_verify: bool,
) -> str:
    if insecure_skip_tls_verify:
        return _download_python_command(
            config,
            repo_id,
            revision,
            file_path,
            temp_dir,
            forwarded_token,
            insecure_skip_tls_verify,
        )
    if config.platform == "windows":
        return _download_windows_command(repo_id, revision, file_path, temp_dir, forwarded_token)
    return _download_linux_command(repo_id, revision, file_path, temp_dir, forwarded_token)


def _download_python_command(
    config: ServerConfig,
    repo_id: str,
    revision: str,
    file_path: str,
    temp_dir: str,
    forwarded_token: str | None,
    insecure_skip_tls_verify: bool,
) -> str:
    encoded = base64.b64encode(_PYTHON_HF_DOWNLOAD_SCRIPT.encode("utf-8")).decode("ascii")
    env = download_python_env(
        repo_id,
        revision,
        file_path,
        temp_dir,
        forwarded_token,
        insecure_skip_tls_verify,
    )
    return python_script_command(config, encoded, env)


def download_python_env(
    repo_id: str,
    revision: str,
    file_path: str,
    temp_dir: str,
    forwarded_token: str | None,
    insecure_skip_tls_verify: bool,
) -> dict[str, str]:
    env = {
        "MSDL_REPO_ID": repo_id,
        "MSDL_REVISION": revision,
        "MSDL_FILE_PATH": file_path,
        "MSDL_LOCAL_DIR": temp_dir,
    }
    if forwarded_token:
        env["MSDL_HF_TOKEN"] = forwarded_token
    if insecure_skip_tls_verify:
        env["MSDL_INSECURE_SKIP_TLS_VERIFY"] = "1"
        env["HF_HUB_DISABLE_XET"] = "1"
    return env


def _download_linux_command(
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


def _download_windows_command(
    repo_id: str,
    revision: str,
    file_path: str,
    temp_dir: str,
    forwarded_token: str | None,
) -> str:
    remote_path = windows_path_join(temp_dir, file_path)
    token_line = ""
    if forwarded_token:
        token_line = f"$env:HF_TOKEN = {ps_quote(forwarded_token)}"
    script = textwrap.dedent(
        f"""
        $ErrorActionPreference = 'Stop'
        $repoId = {ps_quote(repo_id)}
        $filePath = {ps_quote(file_path)}
        $revision = {ps_quote(revision)}
        $localDir = {ps_quote(temp_dir)}
        New-Item -ItemType Directory -Force -LiteralPath $localDir | Out-Null
        {token_line}

        $python = Get-Command python -ErrorAction SilentlyContinue
        $hfTransferEnabled = $false
        if ($python) {{
          & $python.Source -c 'import hf_transfer' *> $null
          if ($LASTEXITCODE -eq 0) {{ $hfTransferEnabled = $true }}
        }}
        if (-not $hfTransferEnabled -and (Get-Command py -ErrorAction SilentlyContinue)) {{
          & py -3 -c 'import hf_transfer' *> $null
          if ($LASTEXITCODE -eq 0) {{ $hfTransferEnabled = $true }}
        }}
        if ($hfTransferEnabled) {{
          $env:HF_HUB_ENABLE_HF_TRANSFER = '1'
        }}

        $hf = Get-Command hf -ErrorAction SilentlyContinue
        if ($hf) {{
          & $hf.Source download $repoId $filePath --revision $revision --local-dir $localDir
          $code = $LASTEXITCODE
        }} else {{
          $hf = Get-Command huggingface-cli -ErrorAction SilentlyContinue
          if (-not $hf) {{ throw 'missing hf or huggingface-cli on worker' }}
          & $hf.Source download $repoId $filePath --revision $revision --local-dir $localDir
          $code = $LASTEXITCODE
        }}
        if ($code -ne 0) {{ exit $code }}
        if (-not (Test-Path -LiteralPath {ps_quote(remote_path)} -PathType Leaf)) {{
          throw 'download did not produce expected file: {remote_path}'
        }}
        """
    )
    return powershell_command(script)


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


def push_file_to_remote(
    local_path: Path,
    ssh_target: str,
    remote_path: str,
    ssh_options: list[str],
    transfer_backend: str,
) -> None:
    remote_part = f"{remote_path}.part"
    mkdir_remote_parent(ssh_target, remote_path, ssh_options)
    if transfer_backend == "rsync":
        push_file_rsync(local_path, ssh_target, remote_part, ssh_options)
    elif transfer_backend == "scp":
        push_file_scp(local_path, ssh_target, remote_part, ssh_options)
    else:
        raise ValueError(f"unsupported transfer backend: {transfer_backend}")
    size = remote_file_size(ssh_target, remote_part, ssh_options)
    if size != local_path.stat().st_size:
        raise RuntimeError(
            f"size mismatch for remote {remote_part}: expected {local_path.stat().st_size}, got {size}"
        )
    run_ssh(
        ssh_target,
        f"mv -f {shlex.quote(remote_part)} {shlex.quote(remote_path)}",
        ssh_options,
        timeout=30,
    )


def push_file_rsync(
    local_path: Path,
    ssh_target: str,
    remote_path: str,
    ssh_options: list[str],
) -> None:
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
        str(local_path),
        f"{ssh_target}:{remote_path}",
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"rsync failed to {ssh_target}:{remote_path}: {proc.stderr.strip()}")


def push_file_scp(
    local_path: Path,
    ssh_target: str,
    remote_path: str,
    ssh_options: list[str],
) -> None:
    cmd = [
        "scp",
        *ssh_options,
        "-p",
        str(local_path),
        f"{ssh_target}:{remote_path}",
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"scp failed to {ssh_target}:{remote_path}: {proc.stderr.strip()}")


def resolve_transfer_backend(requested: str) -> str:
    return resolve_transfer_backend_for_server(requested, None)


def resolve_transfer_backend_for_server(requested: str, config: ServerConfig | None) -> str:
    if config is not None and config.platform == "windows":
        if requested == "rsync":
            raise RuntimeError(
                f"server {config.name} is a Windows worker; use scp or auto transfer backend"
            )
        if shutil.which("scp"):
            return "scp"
        raise RuntimeError("scp is required on the controller for Windows workers")
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
    if config.local:
        Path(remote_path).unlink(missing_ok=True)
        return
    if config.platform == "windows":
        run_windows_powershell(
            require_ssh_target(config),
            f"Remove-Item -LiteralPath {ps_quote(remote_path)} -Force -ErrorAction SilentlyContinue",
            ssh_options,
            timeout=30,
        )
        return
    run_ssh(require_ssh_target(config), f"rm -f {shlex.quote(remote_path)}", ssh_options, timeout=30)


def remove_remote_path(ssh_target: str, remote_path: str, ssh_options: list[str]) -> None:
    run_ssh(ssh_target, f"rm -f {shlex.quote(remote_path)}", ssh_options, timeout=30)


def run_windows_powershell(
    target: str,
    script: str,
    ssh_options: list[str],
    timeout: int | None = None,
) -> CommandResult:
    return run_ssh(target, powershell_command(script), ssh_options, timeout=timeout)


def powershell_command(script: str) -> str:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return f"powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}"


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def python_script_command(config: ServerConfig, encoded_script: str, env: dict[str, str]) -> str:
    code = f"import base64; exec(base64.b64decode({encoded_script!r}).decode())"
    if config.platform == "windows":
        env_lines = "\n".join(
            f"$env:{key} = {ps_quote(value)}"
            for key, value in env.items()
        )
        script = textwrap.dedent(
            f"""
            $ErrorActionPreference = 'Stop'
            {env_lines}
            $code = {ps_quote(code)}
            $exitCode = $null
            $python = Get-Command python -ErrorAction SilentlyContinue
            if ($python) {{
              & $python.Source -c $code
              $exitCode = $LASTEXITCODE
            }}
            if (($null -eq $exitCode -or $exitCode -ne 0) -and (Get-Command py -ErrorAction SilentlyContinue)) {{
              & py -3 -c $code
              $exitCode = $LASTEXITCODE
            }}
            if ($null -eq $exitCode) {{
              throw 'missing python on Windows worker'
            }}
            if ($exitCode -ne 0) {{ exit $exitCode }}
            """
        )
        return powershell_command(script)

    prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    return f"{prefix} python3 -c {shlex.quote(code)}"


def remote_path_for_repo_file(config: ServerConfig, temp_dir: str, file_path: str) -> str:
    if config.platform == "windows":
        return windows_path_join(temp_dir, file_path)
    return posixpath.join(temp_dir, file_path)


def remote_destination_path(root: str, repo_id: str, file_path: str | None = None) -> str:
    namespace, name = validate_repo_id(repo_id)
    path = posixpath.join(root.rstrip("/") or "/", namespace, name)
    if file_path:
        path = posixpath.join(path, file_path)
    return path


def mkdir_remote_parent(ssh_target: str, remote_path: str, ssh_options: list[str]) -> None:
    parent = posixpath.dirname(remote_path)
    run_ssh(ssh_target, f"mkdir -p {shlex.quote(parent)}", ssh_options, timeout=30)


def ensure_remote_directory(ssh_target: str, remote_path: str, ssh_options: list[str]) -> None:
    run_ssh(ssh_target, f"mkdir -p {shlex.quote(remote_path)}", ssh_options, timeout=30)


def remote_file_size(ssh_target: str, remote_path: str, ssh_options: list[str]) -> int | None:
    command = (
        f"if test -f {shlex.quote(remote_path)}; then "
        f"stat -c %s {shlex.quote(remote_path)}; fi"
    )
    result = run_ssh(ssh_target, command, ssh_options, timeout=30)
    output = result.stdout.strip()
    if not output:
        return None
    return int(output)


def remote_free_bytes(ssh_target: str, remote_path: str, ssh_options: list[str]) -> int:
    ensure_remote_directory(ssh_target, remote_path, ssh_options)
    result = run_ssh(
        ssh_target,
        f"df -Pk {shlex.quote(remote_path)} | tail -n 1",
        ssh_options,
        timeout=30,
    )
    parts = result.stdout.strip().split()
    if len(parts) < 4:
        raise RuntimeError(f"could not parse df output for {ssh_target}:{remote_path}")
    return int(parts[3]) * 1024


def require_ssh_target(config: ServerConfig) -> str:
    if not config.ssh_target:
        raise RuntimeError(f"server {config.name} is missing ssh_target")
    return config.ssh_target


def run_local_python_script(
    encoded_script: str,
    env: dict[str, str],
    timeout: int | None = None,
) -> CommandResult:
    code = f"import base64; exec(base64.b64decode({encoded_script!r}).decode())"
    merged_env = os.environ.copy()
    merged_env.update(env)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        env=merged_env,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return CommandResult(stdout=proc.stdout, stderr=proc.stderr)


def windows_path_join(root: str, *parts: str) -> str:
    normalized_root = root.replace("\\", "/")
    base = normalized_root.rstrip("/")
    normalized_parts: list[str] = []
    for part in parts:
        normalized_parts.extend(
            segment
            for segment in part.replace("\\", "/").split("/")
            if segment
        )
    if not normalized_parts:
        if len(normalized_root) == 3 and normalized_root[1:] == ":/":
            return normalized_root
        return base
    return "/".join([base, *normalized_parts])


def windows_path_parent(path: str) -> str:
    normalized = path.replace("\\", "/").rstrip("/")
    if "/" not in normalized:
        return normalized
    parent = normalized.rsplit("/", 1)[0]
    if len(parent) == 2 and parent[1] == ":":
        return f"{parent}/"
    return parent

from __future__ import annotations

import argparse
import concurrent.futures
import errno
import json
import logging
import os
import posixpath
import re
import shlex
import shutil
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from pathlib import PurePosixPath

from .config import load_servers
from .hf import (
    INSECURE_SKIP_TLS_VERIFY_ENV,
    SAVE_PATH_ENV,
    env_flag,
    get_hf_token,
    list_repo_files,
    local_path_for_repo_file,
    target_dir_for,
)
from .models import Assignment, RepoFile, ServerConfig, ServerProbe
from .planner import assign_files, format_bytes
from .progress import (
    ProgressTracker,
    find_status_file,
    format_status_report,
    load_status,
)
from .ssh import (
    check_worker_tools,
    download_remote_file,
    ensure_remote_directory,
    probe_df,
    probe_speed,
    push_file_to_remote,
    remote_destination_path,
    remote_file_size,
    remote_free_bytes,
    remote_path_for_repo_file,
    remove_remote_path,
    pull_file_scp,
    pull_file_rsync,
    remove_remote_file,
    resolve_transfer_backend_for_server,
    write_worker_probe_file,
)


LOG = logging.getLogger("msdl")
WORK_PATH_ENV = "MULTISERVER_DOWNLOAD_WORK_PATH"


@dataclass(frozen=True)
class Destination:
    label: str
    local_target_dir: Path
    remote_ssh_target: str | None = None
    remote_target_dir: str | None = None

    @property
    def is_remote(self) -> bool:
        return self.remote_ssh_target is not None


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    try:
        if args.command == "download":
            download(args)
        elif args.command == "status":
            status(args)
        else:
            parser.error("missing command")
    except Exception as exc:
        LOG.error("%s", exc)
        if args.verbose:
            raise
        raise SystemExit(1) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="msdl")
    parser.set_defaults(verbose=False)
    sub = parser.add_subparsers(dest="command")

    download_parser = sub.add_parser("download", help="download a Hugging Face repo through SSH workers")
    download_parser.add_argument("repo_id", help="Hugging Face repo id, for example org/model")
    download_parser.add_argument("--servers", type=Path, required=True, help="TOML server config")
    download_parser.add_argument("--revision", default="main", help="repo revision, branch, tag, or commit")
    download_parser.add_argument("--include", action="append", default=[], help="fnmatch include pattern")
    download_parser.add_argument("--exclude", action="append", default=[], help="fnmatch exclude pattern")
    download_parser.add_argument("--save-path", type=Path, default=None, help=f"override {SAVE_PATH_ENV}")
    download_parser.add_argument(
        "--work-path",
        type=Path,
        default=None,
        help=f"local plan/status/relay work root for remote destinations; overrides {WORK_PATH_ENV}",
    )
    download_parser.add_argument(
        "--destination",
        default=None,
        help="remote final save root, for example user@final:/models",
    )
    download_parser.add_argument("--speed-test-mib", type=int, default=64, help="HF bytes to read per server")
    download_parser.add_argument("--skip-speed-test", action="store_true", help="use equal server weights")
    download_parser.add_argument("--reserve-gib", type=float, default=5.0, help="free-space reserve per temp root")
    download_parser.add_argument("--forward-hf-token", action="store_true", help="forward local HF token to workers")
    download_parser.add_argument(
        "--insecure-skip-tls-verify",
        action="store_true",
        help="disable TLS certificate verification for Hugging Face requests",
    )
    download_parser.add_argument("--ssh-option", action="append", default=[], help="extra ssh option, repeatable")
    download_parser.add_argument(
        "--transfer-backend",
        choices=("auto", "rsync", "scp"),
        default="auto",
        help="file transfer backend for worker pulls and destination pushes",
    )
    download_parser.add_argument("--keep-remote", action="store_true", help="keep files in worker temp dirs")
    download_parser.add_argument("--dry-run", action="store_true", help="probe and plan without downloading")
    download_parser.add_argument("-v", "--verbose", action="store_true", help="show debug logs")

    status_parser = sub.add_parser("status", help="show progress for a running or completed job")
    status_parser.add_argument("repo_id", help="Hugging Face repo id, for example org/model")
    status_parser.add_argument("--save-path", type=Path, default=None, help=f"override {SAVE_PATH_ENV}")
    status_parser.add_argument("--work-path", type=Path, default=None, help=f"override {WORK_PATH_ENV}")
    status_parser.add_argument("--job-id", default=None, help="specific job id under .msdl")
    status_parser.add_argument("--watch", nargs="?", const=5.0, type=float, default=None, help="refresh every N seconds")
    status_parser.add_argument("--json", action="store_true", help="print raw status JSON")
    status_parser.add_argument("-v", "--verbose", action="store_true", help="show debug logs")
    return parser


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def download(args: argparse.Namespace) -> None:
    ssh_options = flatten_ssh_options(args.ssh_option)
    destination = resolve_destination(
        repo_id=args.repo_id,
        save_path=args.save_path,
        work_path=args.work_path,
        remote_destination=args.destination,
        ssh_options=ssh_options,
    )
    target_dir = destination.local_target_dir
    incoming_dir = target_dir / ".msdl" / "incoming"
    job_id = uuid.uuid4().hex[:12]
    plan_dir = target_dir / ".msdl" / job_id
    token = get_hf_token()
    forwarded_token = token if args.forward_hf_token else None
    insecure_skip_tls_verify = args.insecure_skip_tls_verify or env_flag(INSECURE_SKIP_TLS_VERIFY_ENV)

    LOG.info("repo: %s", args.repo_id)
    LOG.info("revision: %s", args.revision)
    LOG.info("local work target: %s", target_dir)
    LOG.info("final target: %s", destination.label)
    if insecure_skip_tls_verify:
        LOG.warning("TLS certificate verification is disabled for Hugging Face requests")

    servers = load_servers(args.servers)
    files = list_repo_files(
        args.repo_id,
        args.revision,
        args.include,
        args.exclude,
        token,
        insecure_skip_tls_verify=insecure_skip_tls_verify,
    )
    if not files:
        raise ValueError("manifest matched no files")
    total_bytes = sum(file.size for file in files)
    LOG.info("manifest: %s files, %s", len(files), format_bytes(total_bytes))

    missing_paths = ensure_destination_capacity(destination, files, ssh_options)
    plan_dir.mkdir(parents=True, exist_ok=True)
    incoming_dir.mkdir(parents=True, exist_ok=True)

    probes = probe_servers(
        servers=servers,
        repo_id=args.repo_id,
        revision=args.revision,
        files=files,
        job_id=job_id,
        speed_test_mib=args.speed_test_mib,
        skip_speed_test=args.skip_speed_test,
        ssh_options=ssh_options,
        forwarded_token=forwarded_token,
        insecure_skip_tls_verify=insecure_skip_tls_verify,
    )

    reserve_bytes = int(args.reserve_gib * 1024**3)
    assignments = assign_files(files, probes, reserve_bytes=reserve_bytes)
    log_plan(assignments)
    ensure_controller_work_capacity(destination, assignments, missing_paths)
    transfer_backend = args.transfer_backend
    validate_transfer_backends(assignments, transfer_backend, destination)
    preflight_checks(
        assignments=assignments,
        incoming_dir=incoming_dir,
        destination=destination,
        ssh_options=ssh_options,
        transfer_backend=transfer_backend,
    )
    write_plan(plan_dir / "plan.json", args, job_id, destination.label, total_bytes, assignments)
    LOG.info("transfer backend: %s", transfer_backend)
    tracker = ProgressTracker.create(
        plan_dir / "status.json",
        repo_id=args.repo_id,
        revision=args.revision,
        target_dir=destination.label,
        assignments=assignments,
    )
    LOG.info("status file: %s", tracker.path)

    if args.dry_run:
        LOG.info("dry run complete; no files downloaded")
        return

    run_assignments(
        assignments=assignments,
        repo_id=args.repo_id,
        revision=args.revision,
        target_dir=target_dir,
        incoming_dir=incoming_dir,
        destination=destination,
        ssh_options=ssh_options,
        forwarded_token=forwarded_token,
        insecure_skip_tls_verify=insecure_skip_tls_verify,
        keep_remote=args.keep_remote,
        tracker=tracker,
        transfer_backend=transfer_backend,
    )
    complete_marker = target_dir / ".download-complete.json"
    write_complete_marker(complete_marker, args.repo_id, args.revision, files)
    if destination.is_remote:
        remote_marker = posixpath.join(destination.remote_target_dir or "", ".download-complete.json")
        push_file_to_remote(
            complete_marker,
            destination.remote_ssh_target or "",
            remote_marker,
            ssh_options,
            resolve_transfer_backend_for_server(args.transfer_backend, None),
        )
    LOG.info("complete: %s", destination.label)


def status(args: argparse.Namespace) -> None:
    save_root = resolve_status_root(args.save_path, args.work_path)
    target_dir = target_dir_for(args.repo_id, save_root)

    def print_once() -> str:
        status_file = find_status_file(target_dir, args.job_id)
        payload = load_status(status_file)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(format_status_report(payload))
        return str(payload["state"])

    if args.watch is None:
        print_once()
        return

    while True:
        state = print_once()
        if state in {"complete", "failed"}:
            return
        time.sleep(max(args.watch, 0.5))


def resolve_save_root(save_path: Path | None) -> Path:
    save_path_value = save_path or os.environ.get(SAVE_PATH_ENV)
    if not save_path_value:
        raise ValueError(f"set {SAVE_PATH_ENV} or pass --save-path")
    return Path(save_path_value).expanduser().resolve()


def resolve_work_root(work_path: Path | None, fallback_save_path: Path | None = None) -> Path:
    work_path_value = work_path or os.environ.get(WORK_PATH_ENV) or fallback_save_path
    if not work_path_value:
        work_path_value = Path(".msdl-work")
    return Path(work_path_value).expanduser().resolve()


def resolve_status_root(save_path: Path | None, work_path: Path | None) -> Path:
    if work_path or os.environ.get(WORK_PATH_ENV):
        return resolve_work_root(work_path)
    if save_path or os.environ.get(SAVE_PATH_ENV):
        return resolve_save_root(save_path)
    return resolve_work_root(None)


def resolve_destination(
    repo_id: str,
    save_path: Path | None,
    work_path: Path | None,
    remote_destination: str | None,
    ssh_options: list[str],
) -> Destination:
    if remote_destination:
        ssh_target, remote_root = parse_remote_destination(remote_destination)
        remote_target_dir = remote_destination_path(remote_root, repo_id)
        work_root = resolve_work_root(work_path, save_path)
        local_target_dir = target_dir_for(repo_id, work_root)
        ensure_remote_directory(ssh_target, remote_target_dir, ssh_options)
        return Destination(
            label=f"{ssh_target}:{remote_target_dir}",
            local_target_dir=local_target_dir,
            remote_ssh_target=ssh_target,
            remote_target_dir=remote_target_dir,
        )

    save_root = resolve_save_root(save_path)
    local_target_dir = target_dir_for(repo_id, save_root)
    return Destination(label=str(local_target_dir), local_target_dir=local_target_dir)


def parse_remote_destination(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise ValueError("--destination must use the form user@host:/absolute/path")
    ssh_target, remote_root = value.split(":", 1)
    ssh_target = ssh_target.strip()
    remote_root = remote_root.strip().rstrip("/") or "/"
    if not ssh_target or not remote_root.startswith("/"):
        raise ValueError("--destination must use the form user@host:/absolute/path")
    return ssh_target, remote_root


def ensure_destination_capacity(
    destination: Destination,
    files: list[RepoFile],
    ssh_options: list[str],
) -> set[str]:
    missing_files = destination_missing_files(destination, files, ssh_options)
    missing_paths = {file.path for file in missing_files}
    missing_bytes = sum(file.size for file in missing_files)

    if not destination.is_remote:
        ensure_local_capacity(destination.local_target_dir, missing_bytes)
        return missing_paths

    destination.local_target_dir.mkdir(parents=True, exist_ok=True)
    remote_free = remote_free_bytes(
        destination.remote_ssh_target or "",
        destination.remote_target_dir or "",
        ssh_options,
    )
    if remote_free < missing_bytes:
        raise ValueError(
            f"not enough remote free space under {destination.label}: "
            f"need {format_bytes(missing_bytes)}, have {format_bytes(remote_free)}"
        )
    return missing_paths


def destination_missing_files(
    destination: Destination,
    files: list[RepoFile],
    ssh_options: list[str],
) -> list[RepoFile]:
    missing: list[RepoFile] = []
    for file in files:
        if destination.is_remote:
            remote_path = posixpath.join(destination.remote_target_dir or "", file.path)
            existing_size = remote_file_size(
                destination.remote_ssh_target or "",
                remote_path,
                ssh_options,
            )
            if existing_size == file.size:
                continue
        else:
            local_path = local_path_for_repo_file(destination.local_target_dir, file.path)
            if local_path.exists() and local_path.stat().st_size == file.size:
                continue
        missing.append(file)
    return missing


def ensure_controller_work_capacity(
    destination: Destination,
    assignments: list[Assignment],
    missing_paths: set[str],
) -> None:
    if not destination.is_remote:
        return

    required_bytes = 0
    for assignment in assignments:
        if assignment.probe.config.local:
            continue
        missing_sizes = [
            file.size
            for file in assignment.files
            if file.path in missing_paths
        ]
        if missing_sizes:
            required_bytes += max(missing_sizes)
    ensure_local_capacity(destination.local_target_dir, required_bytes)


def flatten_ssh_options(options: list[str]) -> list[str]:
    flattened: list[str] = []
    for option in options:
        flattened.extend(split_ssh_option(option))
    return flattened


def split_ssh_option(option: str) -> list[str]:
    option = option.strip()
    if not option:
        return []
    if os.name == "nt":
        return split_windows_ssh_option(option)
    return shlex.split(option)


def split_windows_ssh_option(option: str) -> list[str]:
    match = re.match(r"^(-[A-Za-z0-9]+)\s+(.+)$", option)
    if match:
        return [match.group(1), strip_outer_quotes(match.group(2))]
    return [strip_outer_quotes(part) for part in option.split() if part]


def strip_outer_quotes(value: str) -> str:
    if "=" in value:
        key, nested_value = value.split("=", 1)
        return f"{key}={strip_outer_quotes(nested_value)}"
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def ensure_local_capacity(save_root: Path, total_bytes: int) -> None:
    save_root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(save_root)
    if usage.free < total_bytes:
        raise ValueError(
            f"not enough local free space under {save_root}: "
            f"need {format_bytes(total_bytes)}, have {format_bytes(usage.free)}"
        )


def probe_servers(
    servers: list[ServerConfig],
    repo_id: str,
    revision: str,
    files: list[RepoFile],
    job_id: str,
    speed_test_mib: int,
    skip_speed_test: bool,
    ssh_options: list[str],
    forwarded_token: str | None,
    insecure_skip_tls_verify: bool,
) -> list[ServerProbe]:
    sample = max(files, key=lambda item: item.size)
    bytes_to_read = max(1, speed_test_mib) * 1024 * 1024

    def probe_one(server: ServerConfig) -> ServerProbe:
        temp_root, temp_dir, free_bytes = probe_df(server, job_id, repo_id, ssh_options)
        if skip_speed_test:
            speed_bps = 1.0
        else:
            speed_bps = probe_speed(
                server,
                repo_id=repo_id,
                revision=revision,
                sample_path=sample.path,
                bytes_to_read=bytes_to_read,
                ssh_options=ssh_options,
                forwarded_token=forwarded_token,
                insecure_skip_tls_verify=insecure_skip_tls_verify,
            )
        LOG.info(
            "probe %s: temp=%s free=%s speed=%s/s",
            server.name,
            temp_dir,
            format_bytes(free_bytes),
            format_bytes(speed_bps),
        )
        return ServerProbe(
            config=server,
            temp_root=temp_root,
            temp_dir=temp_dir,
            free_bytes=free_bytes,
            speed_bps=speed_bps,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(servers)) as pool:
        return list(pool.map(probe_one, servers))


def log_plan(assignments: list[Assignment]) -> None:
    LOG.info("download plan:")
    for assignment in assignments:
        LOG.info(
            "  %s -> %s files, %s, temp=%s",
            assignment.probe.config.name,
            len(assignment.files),
            format_bytes(assignment.total_bytes),
            assignment.probe.temp_dir,
        )


def validate_transfer_backends(
    assignments: list[Assignment],
    transfer_backend: str,
    destination: Destination,
) -> None:
    for assignment in assignments:
        resolve_transfer_backend_for_server(transfer_backend, assignment.probe.config)
    if destination.is_remote:
        resolve_transfer_backend_for_server(transfer_backend, None)


def preflight_checks(
    assignments: list[Assignment],
    incoming_dir: Path,
    destination: Destination,
    ssh_options: list[str],
    transfer_backend: str,
) -> None:
    incoming_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:8]
    content = f"msdl-preflight-{token}\n"
    LOG.info("preflight: checking worker tools and transfer paths")

    for assignment in assignments:
        config = assignment.probe.config
        check_worker_tools(config, ssh_options)
        remote_probe_path = remote_path_for_repo_file(
            config,
            assignment.probe.temp_dir,
            f".msdl-preflight-{token}.txt",
        )
        local_probe_path = incoming_dir / f".msdl-preflight-{config.name}-{token}.txt"
        backend = resolve_transfer_backend_for_server(transfer_backend, config)
        try:
            write_worker_probe_file(config, remote_probe_path, content, ssh_options)
            if config.local:
                actual = Path(remote_probe_path).read_text(encoding="ascii")
            else:
                if not config.ssh_target:
                    raise RuntimeError(f"server {config.name} is missing ssh_target")
                pull_file(backend, config.ssh_target, remote_probe_path, local_probe_path, ssh_options)
                actual = local_probe_path.read_text(encoding="ascii")
            if actual != content:
                raise RuntimeError(f"preflight probe mismatch for worker {config.name}")
        finally:
            local_probe_path.unlink(missing_ok=True)
            try:
                remove_remote_file(config, remote_probe_path, ssh_options)
            except Exception as exc:
                LOG.debug("preflight cleanup failed for %s: %s", config.name, exc)

    if destination.is_remote:
        destination_backend = resolve_transfer_backend_for_server(transfer_backend, None)
        local_probe_path = incoming_dir / f".msdl-preflight-destination-{token}.txt"
        remote_probe_path = posixpath.join(
            destination.remote_target_dir or "",
            f".msdl-preflight-{token}.txt",
        )
        local_probe_path.write_text(content, encoding="ascii")
        try:
            push_file_to_remote(
                local_probe_path,
                destination.remote_ssh_target or "",
                remote_probe_path,
                ssh_options,
                destination_backend,
            )
        finally:
            local_probe_path.unlink(missing_ok=True)
            try:
                remove_remote_path(destination.remote_ssh_target or "", remote_probe_path, ssh_options)
            except Exception as exc:
                LOG.debug("preflight destination cleanup failed: %s", exc)

    LOG.info("preflight: all checks passed")


def write_plan(
    path: Path,
    args: argparse.Namespace,
    job_id: str,
    target_label: str,
    total_bytes: int,
    assignments: list[Assignment],
) -> None:
    payload = {
        "job_id": job_id,
        "repo_id": args.repo_id,
        "revision": args.revision,
        "target_dir": target_label,
        "total_bytes": total_bytes,
        "assignments": [
            {
                "server": asdict(assignment.probe.config),
                "temp_dir": assignment.probe.temp_dir,
                "free_bytes": assignment.probe.free_bytes,
                "speed_bps": assignment.probe.speed_bps,
                "total_bytes": assignment.total_bytes,
                "files": [asdict(file) for file in assignment.files],
            }
            for assignment in assignments
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    LOG.info("plan file: %s", path)


def run_assignments(
    assignments: list[Assignment],
    repo_id: str,
    revision: str,
    target_dir: Path,
    incoming_dir: Path,
    destination: Destination,
    ssh_options: list[str],
    forwarded_token: str | None,
    insecure_skip_tls_verify: bool,
    keep_remote: bool,
    tracker: ProgressTracker,
    transfer_backend: str,
) -> None:
    stop_event = threading.Event()

    def run_one(assignment: Assignment) -> None:
        probe = assignment.probe
        for file in assignment.files:
            if stop_event.is_set():
                return
            final_path = local_path_for_repo_file(target_dir, file.path)
            remote_final_path = (
                posixpath.join(destination.remote_target_dir or "", file.path)
                if destination.is_remote
                else None
            )
            part_path = local_path_for_repo_file(incoming_dir, file.path).with_name(
                f"{PurePosixPath(file.path).name}.part"
            )
            try:
                if destination.is_remote and remote_final_path:
                    existing_size = remote_file_size(
                        destination.remote_ssh_target or "",
                        remote_final_path,
                        ssh_options,
                    )
                    if existing_size == file.size:
                        summary = tracker.update_file(file.path, "skipped")
                        LOG.info("%s skip existing %s; %s", probe.config.name, file.path, summary)
                        continue
                elif final_path.exists() and final_path.stat().st_size == file.size:
                    summary = tracker.update_file(file.path, "skipped")
                    LOG.info("%s skip existing %s; %s", probe.config.name, file.path, summary)
                    continue

                summary = tracker.update_file(file.path, "downloading")
                LOG.info(
                    "%s download %s (%s); %s",
                    probe.config.name,
                    file.path,
                    format_bytes(file.size),
                    summary,
                )
                remote_path = download_remote_file(
                    probe.config,
                    repo_id=repo_id,
                    revision=revision,
                    file=file,
                    temp_dir=probe.temp_dir,
                    ssh_options=ssh_options,
                    forwarded_token=forwarded_token,
                    insecure_skip_tls_verify=insecure_skip_tls_verify,
                )
                summary = tracker.update_file(file.path, "transferring")
                backend = resolve_transfer_backend_for_server(transfer_backend, probe.config)
                if probe.config.local:
                    transfer_source = Path(remote_path)
                else:
                    if not probe.config.ssh_target:
                        raise RuntimeError(f"server {probe.config.name} is missing ssh_target")
                    LOG.info("%s pull %s via %s; %s", probe.config.name, file.path, backend, summary)
                    pull_file(
                        backend,
                        probe.config.ssh_target,
                        remote_path,
                        part_path,
                        ssh_options,
                    )
                    transfer_source = part_path
                verify_size(transfer_source, file)

                if destination.is_remote and remote_final_path:
                    destination_backend = resolve_transfer_backend_for_server(
                        transfer_backend,
                        None,
                    )
                    LOG.info(
                        "%s push %s to %s via %s; %s",
                        probe.config.name,
                        file.path,
                        destination.label,
                        destination_backend,
                        summary,
                    )
                    push_file_to_remote(
                        transfer_source,
                        destination.remote_ssh_target or "",
                        remote_final_path,
                        ssh_options,
                        destination_backend,
                    )
                    if transfer_source == part_path:
                        part_path.unlink(missing_ok=True)
                else:
                    finalize_local_file(
                        transfer_source,
                        final_path,
                        keep_source=probe.config.local and keep_remote,
                    )
                if not keep_remote:
                    remove_remote_file(probe.config, remote_path, ssh_options)
                summary = tracker.update_file(file.path, "done")
                LOG.info("%s done %s; %s", probe.config.name, file.path, summary)
            except Exception as exc:
                tracker.update_file(file.path, "failed", error=str(exc))
                stop_event.set()
                raise

    tracker.set_state("running")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(assignments)) as pool:
            futures = [pool.submit(run_one, assignment) for assignment in assignments]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception:
                    stop_event.set()
                    for pending in futures:
                        pending.cancel()
                    raise
    except Exception:
        tracker.set_state("failed")
        raise
    tracker.set_state("complete")


def finalize_local_file(source: Path, final_path: Path, keep_source: bool) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if keep_source:
        part_path = final_path.with_name(f"{final_path.name}.part")
        shutil.copy2(source, part_path)
        part_path.replace(final_path)
        return

    try:
        source.replace(final_path)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        part_path = final_path.with_name(f"{final_path.name}.part")
        shutil.copy2(source, part_path)
        part_path.replace(final_path)
        source.unlink(missing_ok=True)


def pull_file(
    transfer_backend: str,
    ssh_target: str,
    remote_path: str,
    part_path: Path,
    ssh_options: list[str],
) -> None:
    if transfer_backend == "rsync":
        pull_file_rsync(ssh_target, remote_path, part_path, ssh_options)
    elif transfer_backend == "scp":
        pull_file_scp(ssh_target, remote_path, part_path, ssh_options)
    else:
        raise ValueError(f"unsupported transfer backend: {transfer_backend}")


def verify_size(path: Path, file: RepoFile) -> None:
    actual = path.stat().st_size
    if actual != file.size:
        raise RuntimeError(
            f"size mismatch for {file.path}: expected {file.size}, got {actual}"
        )


def write_complete_marker(path: Path, repo_id: str, revision: str, files: list[RepoFile]) -> None:
    payload = {
        "repo_id": repo_id,
        "revision": revision,
        "file_count": len(files),
        "total_bytes": sum(file.size for file in files),
        "files": [asdict(file) for file in files],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])

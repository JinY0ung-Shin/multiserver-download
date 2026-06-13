from pathlib import Path

import pytest

from msdl import cli
from msdl.cli import (
    Destination,
    build_parser,
    download,
    ensure_controller_work_capacity,
    ensure_destination_capacity,
    finalize_local_file,
    parse_remote_destination,
    preflight_checks,
    split_windows_ssh_option,
)
from msdl.hf import SAVE_PATH_ENV
from msdl.models import Assignment, RepoFile, ServerConfig, ServerProbe


def test_download_requires_save_path(monkeypatch):
    monkeypatch.delenv(SAVE_PATH_ENV, raising=False)
    args = build_parser().parse_args(["download", "org/model", "--servers", "servers.toml"])

    with pytest.raises(ValueError, match=SAVE_PATH_ENV):
        download(args)


def test_status_command_parses_without_verbose():
    args = build_parser().parse_args(["status", "org/model", "--save-path", "/models"])

    assert args.command == "status"
    assert args.verbose is False
    assert args.save_path == Path("/models")


def test_download_transfer_backend_defaults_to_auto():
    args = build_parser().parse_args(["download", "org/model", "--servers", "servers.toml"])

    assert args.transfer_backend == "auto"


def test_download_parses_insecure_skip_tls_verify():
    args = build_parser().parse_args(
        [
            "download",
            "org/model",
            "--servers",
            "servers.toml",
            "--insecure-skip-tls-verify",
        ]
    )

    assert args.insecure_skip_tls_verify is True


def test_download_parses_remote_destination_and_work_path():
    args = build_parser().parse_args(
        [
            "download",
            "org/model",
            "--servers",
            "servers.toml",
            "--destination",
            "final:/models",
            "--work-path",
            "/tmp/msdl-work",
        ]
    )

    assert args.destination == "final:/models"
    assert args.work_path == Path("/tmp/msdl-work")


def test_parse_remote_destination_requires_absolute_path():
    with pytest.raises(ValueError, match="--destination"):
        parse_remote_destination("final:models")


def test_ensure_destination_capacity_counts_only_missing_local_files(tmp_path, monkeypatch):
    target = tmp_path / "target"
    existing = target / "done.bin"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"abc")
    destination = Destination(label=str(target), local_target_dir=target)
    calls = []

    monkeypatch.setattr(
        cli,
        "ensure_local_capacity",
        lambda path, required: calls.append((path, required)),
    )

    missing = ensure_destination_capacity(
        destination,
        [RepoFile("done.bin", 3), RepoFile("missing.bin", 5)],
        [],
    )

    assert missing == {"missing.bin"}
    assert calls == [(target, 5)]


def test_ensure_destination_capacity_counts_only_missing_remote_files(tmp_path, monkeypatch):
    destination = Destination(
        label="final:/models/org/model",
        local_target_dir=tmp_path / "work",
        remote_ssh_target="final",
        remote_target_dir="/models/org/model",
    )

    def fake_remote_file_size(ssh_target, remote_path, options):
        if remote_path.endswith("/done.bin"):
            return 3
        return None

    monkeypatch.setattr(cli, "remote_file_size", fake_remote_file_size)
    monkeypatch.setattr(cli, "remote_free_bytes", lambda ssh_target, remote_path, options: 5)

    missing = ensure_destination_capacity(
        destination,
        [RepoFile("done.bin", 3), RepoFile("missing.bin", 5)],
        [],
    )

    assert missing == {"missing.bin"}


def test_ensure_controller_work_capacity_sums_concurrent_remote_pulls(tmp_path, monkeypatch):
    destination = Destination(
        label="final:/models/org/model",
        local_target_dir=tmp_path / "work",
        remote_ssh_target="final",
        remote_target_dir="/models/org/model",
    )
    remote1 = Assignment(
        probe=ServerProbe(
            config=ServerConfig("remote1", "user@remote1", ("/tmp",)),
            temp_root="/tmp",
            temp_dir="/tmp/job",
            free_bytes=1000,
            speed_bps=1,
        ),
        files=(RepoFile("a.bin", 100), RepoFile("b.bin", 10)),
    )
    remote2 = Assignment(
        probe=ServerProbe(
            config=ServerConfig("remote2", "user@remote2", ("/tmp",)),
            temp_root="/tmp",
            temp_dir="/tmp/job",
            free_bytes=1000,
            speed_bps=1,
        ),
        files=(RepoFile("c.bin", 70),),
    )
    local = Assignment(
        probe=ServerProbe(
            config=ServerConfig("main", None, ("/tmp",), local=True),
            temp_root="/tmp",
            temp_dir="/tmp/job",
            free_bytes=1000,
            speed_bps=1,
        ),
        files=(RepoFile("local.bin", 1000),),
    )
    calls = []

    monkeypatch.setattr(
        cli,
        "ensure_local_capacity",
        lambda path, required: calls.append((path, required)),
    )

    ensure_controller_work_capacity(
        destination,
        [remote1, remote2, local],
        {"a.bin", "b.bin", "c.bin", "local.bin"},
    )

    assert calls == [(destination.local_target_dir, 170)]


def test_finalize_local_file_falls_back_when_rename_crosses_devices(tmp_path, monkeypatch):
    source = tmp_path / "temp" / "model.bin"
    final = tmp_path / "final" / "model.bin"
    source.parent.mkdir()
    source.write_bytes(b"model")
    original_replace = Path.replace

    def fake_replace(self, target):
        if self == source:
            raise OSError(cli.errno.EXDEV, "cross-device link")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fake_replace)

    finalize_local_file(source, final, keep_source=False)

    assert final.read_bytes() == b"model"
    assert not source.exists()


def test_windows_ssh_option_split_preserves_backslash_path():
    assert split_windows_ssh_option(r"-i C:\Users\me\.ssh\id_ed25519") == [
        "-i",
        r"C:\Users\me\.ssh\id_ed25519",
    ]


def test_windows_ssh_option_split_preserves_spaced_option_value():
    assert split_windows_ssh_option('-o ProxyCommand="ssh jump nc %h %p"') == [
        "-o",
        "ProxyCommand=ssh jump nc %h %p",
    ]


def test_preflight_checks_verifies_worker_pull_and_destination_push(tmp_path, monkeypatch):
    class FixedUUID:
        hex = "abcdef1234567890"

    server = ServerConfig(
        name="linux1",
        ssh_target="user@linux1",
        temp_roots=("/tmp",),
    )
    assignment = Assignment(
        probe=ServerProbe(
            config=server,
            temp_root="/tmp",
            temp_dir="/tmp/msdl/job",
            free_bytes=1024,
            speed_bps=1.0,
        ),
        files=(RepoFile("model.bin", 1),),
    )
    destination = Destination(
        label="final:/models/org/model",
        local_target_dir=tmp_path / "work",
        remote_ssh_target="final",
        remote_target_dir="/models/org/model",
    )
    calls = []
    content = "msdl-preflight-abcdef12\n"

    monkeypatch.setattr(cli.uuid, "uuid4", lambda: FixedUUID())
    monkeypatch.setattr(
        cli,
        "check_worker_tools",
        lambda config, options: calls.append(("tools", config.name)),
    )
    monkeypatch.setattr(
        cli,
        "write_worker_probe_file",
        lambda config, path, probe_content, options: calls.append(
            ("write-worker", path, probe_content)
        ),
    )

    def fake_pull(backend, ssh_target, remote_path, part_path, options):
        calls.append(("pull", backend, ssh_target, remote_path))
        part_path.write_text(content, encoding="ascii")

    def fake_push(local_path, ssh_target, remote_path, options, backend):
        calls.append(("push", backend, ssh_target, remote_path, local_path.read_text(encoding="ascii")))

    monkeypatch.setattr(cli, "pull_file", fake_pull)
    monkeypatch.setattr(cli, "push_file_to_remote", fake_push)
    monkeypatch.setattr(
        cli,
        "remove_remote_file",
        lambda config, path, options: calls.append(("remove-worker", path)),
    )
    monkeypatch.setattr(
        cli,
        "remove_remote_path",
        lambda ssh_target, path, options: calls.append(("remove-destination", path)),
    )
    monkeypatch.setattr(
        cli,
        "resolve_transfer_backend_for_server",
        lambda requested, config: "scp",
    )

    preflight_checks(
        assignments=[assignment],
        incoming_dir=tmp_path / "incoming",
        destination=destination,
        ssh_options=[],
        transfer_backend="auto",
    )

    assert ("tools", "linux1") in calls
    assert any(call[0] == "pull" and call[1] == "scp" for call in calls)
    assert any(call[0] == "push" and call[2] == "final" for call in calls)

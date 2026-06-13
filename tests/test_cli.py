from pathlib import Path

import pytest

from msdl import cli
from msdl.cli import (
    Destination,
    build_parser,
    download,
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

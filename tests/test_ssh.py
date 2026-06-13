import shutil

import pytest

from msdl import ssh
from msdl.models import ServerConfig


def test_auto_transfer_backend_prefers_scp_on_windows(monkeypatch):
    monkeypatch.setattr(ssh.os, "name", "nt")
    monkeypatch.setattr(shutil, "which", lambda name: f"C:/{name}.exe")

    assert ssh.resolve_transfer_backend("auto") == "scp"


def test_auto_transfer_backend_prefers_rsync_on_posix(monkeypatch):
    monkeypatch.setattr(ssh.os, "name", "posix")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name == "rsync" else None,
    )

    assert ssh.resolve_transfer_backend("auto") == "rsync"


def test_auto_transfer_backend_uses_scp_for_windows_worker(monkeypatch):
    server = ServerConfig(
        name="win1",
        ssh_target="user@win1",
        temp_roots=("D:/msdl-tmp",),
        platform="windows",
    )
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/usr/bin/scp" if name == "scp" else None,
    )

    assert ssh.resolve_transfer_backend_for_server("auto", server) == "scp"


def test_rsync_transfer_backend_rejects_windows_worker():
    server = ServerConfig(
        name="win1",
        ssh_target="user@win1",
        temp_roots=("D:/msdl-tmp",),
        platform="windows",
    )

    with pytest.raises(RuntimeError, match="Windows worker"):
        ssh.resolve_transfer_backend_for_server("rsync", server)


def test_windows_remote_path_uses_forward_slashes():
    server = ServerConfig(
        name="win1",
        ssh_target="user@win1",
        temp_roots=("D:/msdl-tmp",),
        platform="windows",
    )

    assert (
        ssh.remote_path_for_repo_file(server, r"D:\msdl-tmp\job", "weights/model.bin")
        == "D:/msdl-tmp/job/weights/model.bin"
    )


def test_remote_destination_path_appends_repo_and_file():
    assert (
        ssh.remote_destination_path("/models", "org/model", "weights/model.bin")
        == "/models/org/model/weights/model.bin"
    )


def test_remote_destination_path_rejects_parent_repo_segments():
    with pytest.raises(ValueError, match="repo_id"):
        ssh.remote_destination_path("/models", "org/..")


def test_auto_transfer_backend_for_local_worker_uses_controller_tools(monkeypatch):
    server = ServerConfig(
        name="main",
        ssh_target=None,
        temp_roots=("/data/msdl-tmp",),
        local=True,
    )
    monkeypatch.setattr(ssh.os, "name", "posix")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name == "rsync" else None,
    )

    assert ssh.resolve_transfer_backend_for_server("auto", server) == "rsync"


def test_check_worker_tools_local_requires_hf_cli(monkeypatch):
    server = ServerConfig(
        name="main",
        ssh_target=None,
        temp_roots=("/tmp/msdl",),
        local=True,
    )
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="missing hf or huggingface-cli"):
        ssh.check_worker_tools(server, [])


def test_check_worker_tools_linux_runs_remote_command(monkeypatch):
    server = ServerConfig(
        name="linux1",
        ssh_target="user@linux1",
        temp_roots=("/tmp/msdl",),
    )
    calls = []

    def fake_run_ssh(target, command, options, timeout=None):
        calls.append((target, command, options, timeout))
        return ssh.CommandResult(stdout="", stderr="")

    monkeypatch.setattr(ssh, "run_ssh", fake_run_ssh)

    ssh.check_worker_tools(server, ["-i", "key"])

    assert calls
    assert calls[0][0] == "user@linux1"
    assert "huggingface-cli" in calls[0][1]


def test_write_worker_probe_file_local(tmp_path):
    server = ServerConfig(
        name="main",
        ssh_target=None,
        temp_roots=(str(tmp_path),),
        local=True,
    )
    probe_file = tmp_path / "probe" / "check.txt"

    ssh.write_worker_probe_file(server, str(probe_file), "ok\n", [])

    assert probe_file.read_text(encoding="ascii") == "ok\n"


def test_windows_path_parent_keeps_drive_root():
    assert ssh.windows_path_parent("D:/probe.txt") == "D:/"
    assert ssh.windows_path_parent("D:/tmp/probe.txt") == "D:/tmp"

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

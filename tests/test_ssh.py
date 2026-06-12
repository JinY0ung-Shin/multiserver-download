import shutil

from msdl import ssh


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

import pytest

from msdl.config import load_servers


def test_load_servers_defaults_to_linux_platform(tmp_path):
    path = tmp_path / "servers.toml"
    path.write_text(
        """
        [[servers]]
        name = "linux1"
        ssh_target = "user@linux1"
        temp_roots = ["/data/tmp/"]
        """,
        encoding="utf-8",
    )

    server = load_servers(path)[0]

    assert server.platform == "linux"
    assert server.temp_roots == ("/data/tmp",)


def test_load_servers_supports_windows_platform(tmp_path):
    path = tmp_path / "servers.toml"
    path.write_text(
        r"""
        [[servers]]
        name = "win1"
        platform = "windows"
        ssh_target = "user@win1"
        temp_roots = ['D:\msdl-tmp\']
        """,
        encoding="utf-8",
    )

    server = load_servers(path)[0]

    assert server.platform == "windows"
    assert server.temp_roots == ("D:/msdl-tmp",)


def test_load_servers_supports_local_main_without_ssh_target(tmp_path):
    path = tmp_path / "servers.toml"
    path.write_text(
        """
        [[servers]]
        name = "main"
        local = true
        temp_roots = ["/data/msdl-tmp"]
        """,
        encoding="utf-8",
    )

    server = load_servers(path)[0]

    assert server.local is True
    assert server.ssh_target is None
    assert server.platform == "linux"


def test_load_servers_rejects_unknown_platform(tmp_path):
    path = tmp_path / "servers.toml"
    path.write_text(
        """
        [[servers]]
        name = "bad1"
        platform = "solaris"
        ssh_target = "user@bad1"
        temp_roots = ["/tmp"]
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported server platform"):
        load_servers(path)

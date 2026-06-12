from pathlib import Path

import pytest

from msdl.cli import build_parser, download, split_windows_ssh_option
from msdl.hf import SAVE_PATH_ENV


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

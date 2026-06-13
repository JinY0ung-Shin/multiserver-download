import sys
import types
from pathlib import Path

import pytest

from msdl.hf import (
    configure_hf_http_client,
    list_repo_files,
    local_path_for_repo_file,
    target_dir_for,
    validate_repo_file_path,
    validate_windows_repo_file_path,
)


def test_target_dir_uses_org_model_layout():
    assert target_dir_for("org/model", Path("/models")) == Path("/models/org/model")


def test_target_dir_rejects_parent_repo_segments():
    with pytest.raises(ValueError, match="repo_id"):
        target_dir_for("org/..", Path("/models"))


def test_target_dir_rejects_nested_repo_id():
    with pytest.raises(ValueError, match="repo_id"):
        target_dir_for("org/model/extra", Path("/models"))


def test_validate_repo_file_path_rejects_parent_escape():
    with pytest.raises(ValueError, match="unsafe"):
        validate_repo_file_path("../secret")


def test_validate_repo_file_path_rejects_backslash_escape():
    with pytest.raises(ValueError, match="unsafe"):
        validate_repo_file_path(r"..\secret")


def test_local_path_for_repo_file_uses_path_parts():
    assert local_path_for_repo_file(Path("/models"), "org/model.bin") == Path(
        "/models/org/model.bin"
    )


def test_windows_repo_file_path_rejects_reserved_names():
    with pytest.raises(ValueError, match="Windows"):
        validate_windows_repo_file_path("weights/CON.bin")


def test_windows_repo_file_path_rejects_invalid_chars():
    with pytest.raises(ValueError, match="Windows"):
        validate_windows_repo_file_path("weights/model:01.bin")


def test_list_repo_files_rejects_missing_size_metadata(monkeypatch):
    class FakeSibling:
        rfilename = "model.bin"
        size = None
        lfs = {}
        blob_id = "abc"

    class FakeApi:
        def __init__(self, token=None):
            self.token = token

        def repo_info(self, repo_id, revision, files_metadata):
            return types.SimpleNamespace(siblings=[FakeSibling()])

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        types.SimpleNamespace(HfApi=FakeApi),
    )

    with pytest.raises(RuntimeError, match="missing size metadata"):
        list_repo_files("org/model", "main", [], [], None)


def test_configure_hf_http_client_disables_verification(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import huggingface_hub.utils as hf_utils

    monkeypatch.setattr(hf_utils, "set_client_factory", lambda factory: factory())
    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(Client=FakeClient))

    configure_hf_http_client(True)

    assert captured["verify"] is False
    assert captured["follow_redirects"] is True

from pathlib import Path

import pytest

from msdl.hf import (
    local_path_for_repo_file,
    target_dir_for,
    validate_repo_file_path,
    validate_windows_repo_file_path,
)


def test_target_dir_uses_org_model_layout():
    assert target_dir_for("org/model", Path("/models")) == Path("/models/org/model")


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

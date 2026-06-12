from pathlib import Path

from msdl.hf import target_dir_for, validate_repo_file_path


def test_target_dir_uses_org_model_layout():
    assert target_dir_for("org/model", Path("/models")) == Path("/models/org/model")


def test_validate_repo_file_path_rejects_parent_escape():
    try:
        validate_repo_file_path("../secret")
    except ValueError as exc:
        assert "unsafe" in str(exc)
    else:
        raise AssertionError("expected ValueError")

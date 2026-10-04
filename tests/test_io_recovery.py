from __future__ import annotations

import pytest

from tkn_azure_blob_note.catalog import Catalog, Operation
from tkn_azure_blob_note.errors import AppError, ConflictError
from tkn_azure_blob_note.io import atomic_bytes, copy_verified, safe_relative
from tkn_azure_blob_note.recovery import recover
from tkn_azure_blob_note.sync import verify


@pytest.mark.parametrize(
    "name",
    [
        "../a",
        "/a",
        "C:/a",
        "a\\b",
        "a//b",
        "CON.png",
        "a/aux",
        "a. ",
        "a:stream",
        "a/../b",
        "a\x00b",
    ],
)
def test_portable_path_protection(name):
    with pytest.raises(AppError):
        safe_relative(name)


def test_atomic_replace_failure_preserves_previous_file(tmp_path, monkeypatch):
    import tkn_azure_blob_note.io as io

    path = tmp_path / "file"
    path.write_bytes(b"previous")

    def failure(*args):
        raise OSError("failed")

    monkeypatch.setattr(io.os, "replace", failure)
    with pytest.raises(OSError):
        atomic_bytes(path, b"new")
    assert path.read_bytes() == b"previous"
    assert not list(tmp_path.glob("*.tmp"))


def test_copy_verifies_collision(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"first")
    target.write_bytes(b"second")
    with pytest.raises(ConflictError):
        copy_verified(source, target)
    assert source.read_bytes() == b"first"
    assert target.read_bytes() == b"second"


def test_prepared_commit_recovery(cfg, source, monkeypatch):
    import tkn_azure_blob_note.images as images

    original_save = Catalog.save

    def failure(*args):
        raise OSError("interrupted after release commit")

    monkeypatch.setattr(Catalog, "save", failure)
    with pytest.raises(OSError):
        with Operation(cfg, "import", False) as operation:
            images.import_images(cfg, [source], operation)
    monkeypatch.setattr(Catalog, "save", original_save)
    with Operation(cfg, "recover", False) as operation:
        result = recover(cfg, operation)
    assert result[0]["status"] == "recovered"
    assert verify(cfg)[0]["status"] == "valid"

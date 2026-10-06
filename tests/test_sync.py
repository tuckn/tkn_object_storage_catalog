from __future__ import annotations

import pytest

from tkn_objstorage_imgcatalog.assets import NoteStore, Operation
from tkn_objstorage_imgcatalog.errors import AppError, ConflictError
from tkn_objstorage_imgcatalog.images import build_images
from tkn_objstorage_imgcatalog.sync import pull, push, status, verify


def run_push(cfg, blobs, **kwargs):
    with Operation(cfg, "push", kwargs.pop("dry_run", False)) as operation:
        return push(cfg, [], operation, blobs, **kwargs)


def run_pull(cfg, blobs, **kwargs):
    with Operation(cfg, "pull", kwargs.pop("dry_run", False)) as operation:
        return pull(cfg, [], operation, blobs, **kwargs)


def test_push_then_repeat_skips(cfg, asset, blobs):
    assert run_push(cfg, blobs)[0]["status"] == "created"
    assert run_push(cfg, blobs)[0]["status"] == "unchanged"
    assert blobs.writes == 1
    assert verify(cfg, blobs=blobs)[0]["status"] == "valid"


def test_remote_update_blocks_push_then_pull_preserves_bytes(cfg, asset, blobs):
    run_push(cfg, blobs)
    blobs.put(asset["relative_path"], b"replacement exact blob content")
    with pytest.raises(ConflictError):
        run_push(cfg, blobs)
    run_pull(cfg, blobs)
    updated = NoteStore(cfg).assets()[0]
    assert NoteStore(cfg).release(updated).read_bytes() == b"replacement exact blob content"
    assert updated["source"] is None
    assert (cfg.data_root / asset["source"]["path"]).exists()


def test_both_changed_conflict_and_explicit_resolution(cfg, asset, blobs):
    run_push(cfg, blobs)
    cfg.conversion["quality"] = 10
    with Operation(cfg, "build", False) as operation:
        build_images(cfg, [], operation)
    blobs.put(asset["relative_path"], b"remote modified independently")
    with pytest.raises(ConflictError):
        run_push(cfg, blobs)
    with pytest.raises(ConflictError):
        run_pull(cfg, blobs)
    run_push(cfg, blobs, overwrite=True, yes=True)
    assert blobs.writes == 2


def test_public_upload_requires_confirmation(cfg, asset, blobs):
    blobs.public = True
    with pytest.raises(AppError, match="confirmation"):
        run_push(cfg, blobs)
    assert blobs.writes == 0
    run_push(cfg, blobs, yes=True)
    assert blobs.writes == 1


def test_remote_only_download_and_original_unknown(cfg, blobs):
    blobs.put("nested/file.webp", b"exact image bytes")
    assert run_pull(cfg, blobs)[0]["status"] == "created"
    record = NoteStore(cfg).assets()[0]
    assert record["source"] is None
    assert NoteStore(cfg).release(record).read_bytes() == b"exact image bytes"
    assert run_pull(cfg, blobs)[0]["status"] == "unchanged"


def test_sync_dry_run_is_readonly(cfg, asset, blobs):
    paths = [cfg.data_root, cfg.state_root]

    def snap():
        return {
            str(p): (p.read_bytes(), p.stat().st_mtime_ns)
            for root in paths
            for p in root.rglob("*")
            if p.is_file()
        }

    before = snap()
    run_push(cfg, blobs, dry_run=True)
    assert blobs.writes == 0
    assert snap() == before
    blobs.put("new.webp", b"new")
    run_pull(cfg, blobs, dry_run=True)
    assert snap() == before


def test_conditional_write_race(cfg, asset, blobs):
    blobs.race = True
    with pytest.raises(ConflictError):
        run_push(cfg, blobs)
    assert NoteStore(cfg).baseline(asset) is None


def test_remote_deleted_is_not_silently_recreated(cfg, asset, blobs):
    run_push(cfg, blobs)
    blobs.values.clear()
    with pytest.raises(ConflictError):
        run_push(cfg, blobs)


def test_untracked_remote_same_bytes_adopted_without_upload(cfg, asset, blobs):
    blobs.put(asset["relative_path"], NoteStore(cfg).release(asset).read_bytes())
    assert run_push(cfg, blobs)[0]["status"] == "unchanged"
    assert blobs.writes == 0
    assert NoteStore(cfg).baseline(asset)


def test_manual_local_edit_not_overwritten(cfg, asset, blobs):
    run_push(cfg, blobs)
    NoteStore(cfg).release(asset).write_bytes(b"user edit")
    with pytest.raises(ConflictError):
        run_pull(cfg, blobs)
    assert NoteStore(cfg).release(asset).read_bytes() == b"user edit"


def test_malicious_remote_path_rejected(cfg, blobs, tmp_path):
    blobs.put("../outside.webp", b"unsafe")
    with pytest.raises(AppError):
        run_pull(cfg, blobs)
    assert not (tmp_path / "outside.webp").exists()


def test_status_reports_remote_only(cfg, blobs):
    blobs.put("only.webp", b"remote only")
    assert status(cfg, blobs=blobs)[0]["remote"] == "remote_only"


def test_pull_replaces_without_creating_image_archive(cfg, asset, blobs):
    with Operation(cfg, "push", False) as operation:
        push(cfg, [], operation, blobs)
    blobs.put(asset["relative_path"], b"updated remote bytes")
    with Operation(cfg, "pull", False) as operation:
        pull(cfg, [], operation, blobs)
    updated = NoteStore(cfg).assets()[0]
    assert updated["source"] is None
    assert NoteStore(cfg).release(updated).read_bytes() == b"updated remote bytes"
    assert not (cfg.data_root / "history").exists()
    assert not (cfg.data_root / "catalog").exists()
    assert not (cfg.data_root / "provenance").exists()
    assert verify(cfg, blobs=blobs)[0]["status"] == "valid"

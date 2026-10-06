from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tkn_objstorage_imgcatalog.assets import NoteStore, Operation
from tkn_objstorage_imgcatalog.errors import AppError, ConflictError
from tkn_objstorage_imgcatalog.io import atomic_json
from tkn_objstorage_imgcatalog.migration import migrate
from tkn_objstorage_imgcatalog.notes import find_note, serialize, split_note
from tkn_objstorage_imgcatalog.sync import verify


def snapshot(root):
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


def legacy(cfg, asset):
    note = find_note(cfg, asset)
    data, body = split_note(note.read_text(encoding="utf-8"))
    data["schemaVersion"] = "2.0.0"
    data["description"] = "Reviewed description"
    data["customField"] = "Keep me"
    for key in (
        "created",
        "conversionRecipe",
        "releaseGeneratedAt",
        "sourceCapturedAt",
        "acquiredFrom",
    ):
        data.pop(key, None)
    note.write_text(serialize(data, body + "\n## User text\nKeep this body.\n"), encoding="utf-8")
    atomic_json(cfg.data_root / "catalog" / (asset["asset_id"] + ".json"), asset)
    run = next((cfg.state_root / "runs").glob("*.json"))
    old_run = cfg.data_root / "provenance" / run.name
    old_run.parent.mkdir(parents=True, exist_ok=True)
    old_run.write_bytes(run.read_bytes())
    return note, old_run, run


def test_migrate_preview_then_apply_retains_ids_content_originals_and_sync(cfg, asset, tmp_path):
    note, old_run, run = legacy(cfg, asset)
    old_note = note.read_bytes()
    old_record = (cfg.data_root / "catalog" / (asset["asset_id"] + ".json")).read_bytes()
    NoteStore(cfg).set_baseline(asset, {"etag": "existing"})
    original = cfg.data_root / asset["source"]["path"]
    release = cfg.data_root / "releases" / asset["relative_path"]
    image_hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (original, release)]
    before = snapshot(tmp_path)
    with Operation(cfg, "migrate", True) as operation:
        planned = migrate(cfg, operation)
    assert planned["assets"] == planned["notes_updated"] == 1
    assert planned["runs_copied"] == 0
    assert snapshot(tmp_path) == before
    with Operation(cfg, "migrate", False) as operation:
        assert migrate(cfg, operation)["status"] == "migrated"
        backup = cfg.state_root / "migrations" / operation.run_id
    assert not old_run.exists() and run.exists()
    assert not (cfg.data_root / "catalog").exists()
    assert not (cfg.data_root / "provenance").exists()
    assert (backup / "notes" / asset["note_path"]).read_bytes() == old_note
    assert (backup / "data/catalog" / (asset["asset_id"] + ".json")).read_bytes() == old_record
    result = NoteStore(cfg).assets()[0]
    assert {k: v for k, v in result.items() if k != "updated_at"} == {
        k: v for k, v in asset.items() if k != "updated_at"
    }
    assert NoteStore(cfg).baseline(result)["etag"] == "existing"
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in (original, release)] == image_hashes
    content = note.read_text(encoding="utf-8")
    assert (
        "Reviewed description" in content and "Keep me" in content and "Keep this body." in content
    )
    assert verify(cfg)[0]["status"] == "valid"
    with Operation(cfg, "migrate", True) as operation:
        assert migrate(cfg, operation)["status"] == "unchanged"


def test_migrate_copies_missing_run_and_leaves_existing_old_image_archive(cfg, asset):
    _, old_run, run = legacy(cfg, asset)
    payload = old_run.read_bytes()
    run.unlink()
    old_image = cfg.data_root / "history" / "retained.webp"
    old_image.parent.mkdir()
    old_image.write_bytes(b"an existing archive is not silently deleted")
    with Operation(cfg, "migrate", False) as operation:
        assert migrate(cfg, operation)["runs_copied"] == 1
    assert run.read_bytes() == payload
    assert old_image.read_bytes() == b"an existing archive is not silently deleted"


def test_migrate_rejects_conflicting_run_before_changing_notes(cfg, asset, tmp_path):
    note, _, run = legacy(cfg, asset)
    value = json.loads(run.read_text(encoding="utf-8"))
    value["status"] = "failed"
    atomic_json(run, value)
    before = snapshot(tmp_path)
    with pytest.raises(ConflictError, match="same run ID"):
        with Operation(cfg, "migrate", True) as operation:
            migrate(cfg, operation)
    assert snapshot(tmp_path) == before
    assert "2.0.0" in note.read_text(encoding="utf-8")


def test_migrate_rejects_modified_image_and_missing_note(cfg, asset):
    note, _, _ = legacy(cfg, asset)
    note.unlink()
    with pytest.raises(AppError, match="missing"):
        with Operation(cfg, "migrate", True) as operation:
            migrate(cfg, operation)


def test_migrate_rollback_restores_notes_and_old_records(cfg, asset, monkeypatch):
    note, old_run, _ = legacy(cfg, asset)
    before_note = note.read_bytes()
    catalog_path = cfg.data_root / "catalog" / (asset["asset_id"] + ".json")
    original_unlink = Path.unlink

    def fail_once(path, *args, **kwargs):
        if path == old_run:
            raise OSError("simulated interruption during old record retirement")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    with pytest.raises(OSError):
        with Operation(cfg, "migrate", False) as operation:
            migrate(cfg, operation)
    assert note.read_bytes() == before_note
    assert catalog_path.exists() and old_run.exists()
    monkeypatch.setattr(Path, "unlink", original_unlink)
    with Operation(cfg, "migrate", False) as operation:
        assert migrate(cfg, operation)["status"] == "migrated"


def test_migration_uses_renamed_note_and_preserves_flat_custom_metadata(cfg, asset):
    note, _, _ = legacy(cfg, asset)
    moved = note.with_name("renamed.md")
    note.rename(moved)
    with Operation(cfg, "migrate", False) as operation:
        migrate(cfg, operation)
    assert NoteStore(cfg).assets()[0]["note_path"] == "renamed.md"
    assert not note.exists()
    data, _ = split_note(moved.read_text(encoding="utf-8"))
    assert data["customField"] == "Keep me"
    assert data["conversionRecipe"] == asset["release"]["recipe"]


def test_legacy_note_without_record_cannot_be_silently_accepted(cfg, asset):
    note, _, _ = legacy(cfg, asset)
    (cfg.data_root / "catalog" / (asset["asset_id"] + ".json")).unlink()
    with pytest.raises(AppError, match="schema"):
        with Operation(cfg, "migrate", True) as operation:
            migrate(cfg, operation)
    assert "2.0.0" in note.read_text(encoding="utf-8")


def test_migrate_blocks_changed_image_before_any_write(cfg, asset, tmp_path):
    legacy(cfg, asset)
    release = cfg.data_root / "releases" / asset["relative_path"]
    release.write_bytes(b"manually changed")
    before = snapshot(tmp_path)
    with pytest.raises(ConflictError, match="differs"):
        with Operation(cfg, "migrate", True) as operation:
            migrate(cfg, operation)
    assert snapshot(tmp_path) == before


def test_new_management_key_cannot_overwrite_custom_property(cfg, asset):
    note, _, _ = legacy(cfg, asset)
    data, body = split_note(note.read_text(encoding="utf-8"))
    data["created"] = "a user property with a different meaning"
    note.write_text(serialize(data, body), encoding="utf-8")
    before = note.read_bytes()
    with pytest.raises(AppError, match="Invalid image"):
        with Operation(cfg, "migrate", True) as operation:
            migrate(cfg, operation)
    assert note.read_bytes() == before


def test_migration_can_resume_after_new_note_was_written(cfg, asset):
    note, _, _ = legacy(cfg, asset)
    from tkn_objstorage_imgcatalog.notes import render_note

    note.write_bytes(
        render_note(cfg, asset, note, note.read_text(encoding="utf-8")).encode("utf-8")
    )
    with Operation(cfg, "migrate", False) as operation:
        result = migrate(cfg, operation)
    assert result["assets"] == 1 and result["notes_updated"] == 0
    current = NoteStore(cfg).assets()[0]
    assert {k: v for k, v in current.items() if k != "updated_at"} == {
        k: v for k, v in asset.items() if k != "updated_at"
    }


def test_migration_without_original_does_not_emit_legacy_properties(cfg, asset):
    asset["source"] = None
    asset["legacy_source_ref"] = "file:///C:/path/to/old/photo.webp"
    asset["source_unavailable_reason"] = "original_not_available"
    asset["release"]["recipe"] = None
    note, _, _ = legacy(cfg, asset)
    with Operation(cfg, "migrate", False) as operation:
        migrate(cfg, operation)
    actual = NoteStore(cfg).assets()[0]
    assert actual["source"] is None
    assert "legacy_source_ref" not in actual
    assert "source_unavailable_reason" not in actual
    data, _ = split_note(note.read_text(encoding="utf-8"))
    assert data["sourceAvailable"] is False
    assert "sourceUnavailableReason" not in data
    assert "legacySourceRef" not in data


def test_migration_nested_notes(cfg, asset):
    note, _, _ = legacy(cfg, asset)
    nested = cfg.notes_root / "organized"
    nested.mkdir()
    note.rename(nested / note.name)
    with Operation(cfg, "migrate", False) as operation:
        migrate(cfg, operation)
    assert NoteStore(cfg).assets()[0]["asset_id"] == asset["asset_id"]
    assert verify(cfg)[0]["status"] == "valid"


def test_cli_migrate_preview_and_apply(cfg, asset, capsys, tmp_path):
    from tkn_objstorage_imgcatalog.cli import main

    legacy(cfg, asset)
    args = [
        "migrate",
        "--source",
        "my-obj-storage-1",
        "--data-root",
        str(cfg.data_root),
        "--state-root",
        str(cfg.state_root),
    ]
    before = snapshot(tmp_path)
    assert main(args + ["--dry-run"]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["run_id"] is None and planned["result"]["assets"] == 1
    assert snapshot(tmp_path) == before
    assert main(args) == 0
    actual = json.loads(capsys.readouterr().out)
    assert actual["result"]["status"] == "migrated"
    assert NoteStore(cfg).assets()[0]["asset_id"] == asset["asset_id"]

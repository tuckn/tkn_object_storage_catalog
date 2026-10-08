from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest

from tkn_objstorage_imgcatalog.assets import NoteStore, Operation
from tkn_objstorage_imgcatalog.errors import AppError, ConflictError
from tkn_objstorage_imgcatalog.io import atomic_json
from tkn_objstorage_imgcatalog.notes import find_note, serialize, split_note
from tkn_objstorage_imgcatalog.sync import verify


@pytest.mark.parametrize(
    "field,value",
    [
        ("assetId", 1),
        ("schemaVersion", []),
        ("sourceCapturedAt", "not-a-date"),
        ("noteId", "not-a-uuid"),
        ("releaseRef", "../outside"),
        ("sha256", "bad"),
        ("bytes", True),
        ("sourceAvailable", "false"),
        ("originalRef", "3_releases/not-an-original.webp"),
        ("conversionRecipe", 1),
        ("created", "not-a-date"),
        ("releaseGeneratedAt", None),
    ],
)
def test_malformed_managed_frontmatter_stops_read(cfg, asset, field, value):
    note = find_note(cfg, asset)
    data, body = split_note(note.read_text(encoding="utf-8"))
    data[field] = value
    note.write_text(serialize(data, body), encoding="utf-8")
    with pytest.raises(AppError):
        NoteStore(cfg).assets()


def test_notes_are_sufficient_and_unrelated_notes_are_ignored(cfg, asset):
    assert not (cfg.data_root / "catalog").exists()
    assert not (cfg.data_root / "provenance").exists()
    assert not (cfg.data_root / "history").exists()
    assert len(list((cfg.state_root / "runs").glob("*.json"))) == 1
    (cfg.notes_root / "readme.md").write_text("# My notes", encoding="utf-8")
    before = NoteStore(cfg).assets()[0]
    assert before == asset
    assert verify(cfg)[0]["status"] == "valid"


@pytest.mark.parametrize("duplicate", ["assetId", "noteId", "releaseRef"])
def test_duplicate_identifiers_or_release_path_are_rejected(cfg, asset, duplicate):
    note = find_note(cfg, asset)
    data, body = split_note(note.read_text(encoding="utf-8"))
    for key in ("assetId", "noteId"):
        if key != duplicate:
            data[key] = str(uuid4())
    if duplicate != "releaseRef":
        data["releaseRef"] = "3_releases/other.webp"
    (cfg.notes_root / "copy.md").write_text(serialize(data, body), encoding="utf-8")
    with pytest.raises(ConflictError, match="Multiple notes"):
        NoteStore(cfg).assets()


def test_missing_note_does_not_silently_claim_valid_release(cfg, asset):
    find_note(cfg, asset).unlink()
    assert verify(cfg)[0]["status"] == "failed"
    assert verify(cfg)[0]["issues"] == ["Unmanaged release."]


def test_legacy_records_are_rejected(cfg, asset):
    atomic_json(cfg.data_root / "catalog" / (asset["asset_id"] + ".json"), asset)
    with pytest.raises(AppError, match="Old storage layout is not supported"):
        NoteStore(cfg).assets()


def test_different_source_keeps_separate_sync_baseline(cfg, asset):
    other = deepcopy(cfg)
    other.source_values["azure"]["prefix"] = "different/"
    NoteStore(cfg).set_baseline(asset, {"etag": "1"})
    assert NoteStore(other).baseline(asset) is None
    with Operation(cfg, "inspect", True):
        assert NoteStore(cfg).assets()[0]["asset_id"] == asset["asset_id"]

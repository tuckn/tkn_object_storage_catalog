from __future__ import annotations

from copy import deepcopy

import pytest

import tkn_objstorage_imgcatalog.assets as assets
import tkn_objstorage_imgcatalog.images as images
import tkn_objstorage_imgcatalog.notes as notes
import tkn_objstorage_imgcatalog.sync as sync
from tkn_objstorage_imgcatalog.assets import NoteStore, Operation
from tkn_objstorage_imgcatalog.recovery import recover, release_time

IMAGE_TIME = "2026-10-01T01:00:00+00:00"
NOTE_TIME = "2026-10-07T02:00:00+00:00"
LATER = "2026-10-08T03:00:00+00:00"


def read_note(cfg, record):
    path = notes.find_note(cfg, record)
    return path, notes.split_note(path.read_text(encoding="utf-8"))[0]


def test_new_note_dates_are_creation_time_not_image_time(cfg, source, monkeypatch):
    monkeypatch.setattr(assets, "now", lambda: IMAGE_TIME)
    monkeypatch.setattr(images, "now", lambda: IMAGE_TIME)
    monkeypatch.setattr(notes, "now", lambda: NOTE_TIME)
    with Operation(cfg, "import", False) as operation:
        images.import_images(cfg, [source], operation)
    record = NoteStore(cfg).assets()[0]
    _, data = read_note(cfg, record)
    assert data["created"] == data["updated"] == NOTE_TIME
    assert data["releaseGeneratedAt"] == data["sourceCapturedAt"] == IMAGE_TIME
    assert record["created_at"] == record["updated_at"] == NOTE_TIME


def test_refresh_dates_change_only_on_actual_note_change(cfg, asset, monkeypatch):
    path, before = read_note(cfg, asset)
    monkeypatch.setattr(notes, "now", lambda: NOTE_TIME)
    raw = path.read_bytes()
    stamp = path.stat().st_mtime_ns
    assert notes.refresh_notes(cfg, [])[0]["status"] == "unchanged"
    assert path.read_bytes() == raw and path.stat().st_mtime_ns == stamp
    cfg.delivery["url_base"] = "https://images.example.com"
    assert notes.refresh_notes(cfg, [], dry_run=True)[0]["status"] == "updated"
    assert path.read_bytes() == raw and path.stat().st_mtime_ns == stamp
    assert notes.refresh_notes(cfg, [])[0]["status"] == "updated"
    _, after = read_note(cfg, asset)
    assert after["created"] == before["created"]
    assert after["updated"] == NOTE_TIME
    assert after["releaseGeneratedAt"] == before["releaseGeneratedAt"]
    assert after["sourceCapturedAt"] == before["sourceCapturedAt"]
    monkeypatch.setattr(notes, "now", lambda: LATER)
    assert notes.refresh_notes(cfg, [])[0]["status"] == "unchanged"
    assert read_note(cfg, asset)[1]["updated"] == NOTE_TIME


def test_push_updates_note_status_date_without_changing_image_date(cfg, asset, blobs, monkeypatch):
    _, before = read_note(cfg, asset)
    monkeypatch.setattr(notes, "now", lambda: NOTE_TIME)
    with Operation(cfg, "push", False) as operation:
        sync.push(cfg, [], operation, blobs)
    _, after = read_note(cfg, asset)
    assert after["syncStatus"] == "synced"
    assert after["created"] == before["created"]
    assert after["updated"] == NOTE_TIME
    assert after["releaseGeneratedAt"] == before["releaseGeneratedAt"]
    monkeypatch.setattr(notes, "now", lambda: LATER)
    with Operation(cfg, "push", False) as operation:
        sync.push(cfg, [], operation, blobs)
    assert read_note(cfg, asset)[1]["updated"] == NOTE_TIME


@pytest.mark.parametrize("command", ["build", "pull"])
def test_image_and_note_update_dates_are_independent(cfg, asset, blobs, monkeypatch, command):
    _, before = read_note(cfg, asset)
    monkeypatch.setattr(images, "now", lambda: IMAGE_TIME)
    monkeypatch.setattr(sync, "now", lambda: IMAGE_TIME)
    monkeypatch.setattr(notes, "now", lambda: NOTE_TIME)
    with Operation(cfg, command, False) as operation:
        if command == "build":
            cfg.conversion["quality"] = 20
            images.build_images(cfg, [], operation)
        else:
            blobs.put(asset["relative_path"], b"new remote contents")
            sync.pull(cfg, [], operation, blobs, overwrite=True, yes=True)
    _, after = read_note(cfg, asset)
    assert after["created"] == before["created"]
    assert after["updated"] == NOTE_TIME
    assert after["releaseGeneratedAt"] == IMAGE_TIME


def test_recovery_not_suppressed_by_recent_note_edit(cfg, source, monkeypatch):
    monkeypatch.setattr(assets, "now", lambda: IMAGE_TIME)
    monkeypatch.setattr(images, "now", lambda: IMAGE_TIME)
    monkeypatch.setattr(notes, "now", lambda: NOTE_TIME)
    with Operation(cfg, "import", False) as operation:
        images.import_images(cfg, [source], operation)
    asset = NoteStore(cfg).assets()[0]
    path, data = read_note(cfg, asset)
    # The note was edited after the interrupted image generation.
    data["updated"] = LATER
    _, body = notes.split_note(path.read_text(encoding="utf-8"))
    path.write_text(notes.serialize(data, body + "\nUser annotation.\n"), encoding="utf-8")
    original_save = NoteStore.save
    monkeypatch.setattr(images, "now", lambda: "2026-10-07T04:00:00+00:00")
    monkeypatch.setattr(notes, "now", lambda: "2026-10-09T05:00:00+00:00")
    cfg.conversion["quality"] = 20

    def fail(*args, **kwargs):
        raise OSError("Interrupted before saving note")

    monkeypatch.setattr(NoteStore, "save", fail)
    with pytest.raises(OSError):
        with Operation(cfg, "build", False) as operation:
            images.build_images(cfg, [], operation)
    monkeypatch.setattr(NoteStore, "save", original_save)
    with Operation(cfg, "recover", False) as operation:
        result = recover(cfg, operation)
    assert result[0]["status"] == "recovered"
    assert "User annotation." in path.read_text(encoding="utf-8")
    current = NoteStore(cfg).assets()[0]
    assert current["created_at"] == asset["created_at"]
    assert current["release"]["generated_at"] == "2026-10-07T04:00:00+00:00"
    assert sync.verify(cfg)[0]["status"] == "valid"


def test_recovery_image_order_respects_timezone_not_note_date(asset):
    first, second = deepcopy(asset), deepcopy(asset)
    first["updated_at"] = LATER
    second["updated_at"] = IMAGE_TIME
    first["release"]["generated_at"] = "2026-10-07T09:00:00+09:00"
    second["release"]["generated_at"] = "2026-10-07T01:00:00+00:00"
    assert release_time(first) < release_time(second)

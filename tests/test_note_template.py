from __future__ import annotations

from datetime import datetime

import pytest
from ruamel.yaml.comments import CommentedMap

import tkn_objstorage_imgcatalog.note_template as layout
from tkn_objstorage_imgcatalog.assets import NoteStore
from tkn_objstorage_imgcatalog.config import resource
from tkn_objstorage_imgcatalog.errors import AppError
from tkn_objstorage_imgcatalog.notes import (
    find_note,
    format_existing_note,
    refresh_notes,
    serialize,
    split_note,
)


def test_generated_note_layout(cfg, asset):
    text = find_note(cfg, asset).read_text(encoding="utf-8")
    data, body = split_note(text)
    assert list(data)[:5] == ["type", "schemaVersion", "title", "description", "cover"]
    assert list(data)[-4:] == ["tags", "created", "updated", "noteId"]
    assert data["title"] == "example.webp"
    assert data["description"] is None
    assert not {"category", "nouns", "domains", "projects", "published", "publicUrl"} & data.keys()
    assert "# --- Original and provenance ---" in text
    assert "# --- Publication metadata ---" not in text
    assert '\n---\n\n# "' not in text
    assert "\n---\n\n# example.webp\n" in text
    assert body.startswith("\n# example.webp")
    assert format_existing_note(text) == text


def test_template_reordering_controls_existing_and_generated_notes(cfg, asset, monkeypatch):
    template = resource("note.md").replace("sha256:\nbytes:", "bytes:\nsha256:")
    monkeypatch.setattr(layout, "resource", lambda _: template)
    note = find_note(cfg, asset)
    existing = format_existing_note(note.read_text(encoding="utf-8"))
    assert existing.index("bytes:") < existing.index("sha256:")
    refresh_notes(cfg, [])
    refreshed = note.read_text(encoding="utf-8")
    assert refreshed.index("bytes:") < refreshed.index("sha256:")
    before, body = split_note(existing)
    after, new_body = split_note(refreshed)
    assert body == new_body
    assert {k: v for k, v in after.items() if k != "updated"} == {
        k: v for k, v in before.items() if k != "updated"
    }
    assert after["updated"] != before["updated"]


def test_formatting_retains_values_comments_custom_properties_and_body(cfg, asset):
    data, body = split_note(find_note(cfg, asset).read_text(encoding="utf-8"))
    data["description"] = "User description"
    data.yaml_add_eol_comment("keep this comment", key="description")
    data["category"] = "old-folder"
    data["nouns"] = ["[[A concept]]"]
    data["domains"] = []
    data["projects"] = []
    data["status"] = "published"
    data["published"] = datetime.fromisoformat("2026-06-21T08:16:38+09:00")
    data["lastModified"] = datetime.fromisoformat("2026-06-21T08:16:36-05:00")
    data["publicUrl"] = "https://images.example.com/photo.webp"
    data["customRelation"] = "[[Other note]]"
    data["customPath"] = r"C:\path\to\a user's image.webp"
    body += "\n## User notes\nKeep this exactly.\n"
    before = serialize(data, body)
    result = format_existing_note(before)
    after, after_body = split_note(result)
    assert after_body == body
    for key, value in data.items():
        if key not in {"category", "domains", "projects"}:
            assert after[key] == (value.isoformat() if isinstance(value, datetime) else value)
    assert not {"category", "domains", "projects"} & after.keys()
    assert list(after).index("customRelation") < list(after).index("tags")
    assert "# keep this comment" in result
    assert r"customPath: 'C:\path\to\a user''s image.webp'" in result
    assert result == format_existing_note(result)
    assert result.count("# --- Publication metadata ---") == 1


def test_unquoted_managed_datetimes_are_accepted(cfg, asset):
    note = find_note(cfg, asset)
    data, body = split_note(note.read_text(encoding="utf-8"))
    for key in ("created", "updated", "sourceCapturedAt", "releaseGeneratedAt"):
        assert isinstance(data[key], str)
    raw = serialize(data, body)
    for key in ("created", "updated", "sourceCapturedAt", "releaseGeneratedAt"):
        raw = raw.replace(f'{key}: "{data[key]}"', f"{key}: {data[key]}")
    note.write_text(raw, encoding="utf-8")
    record = NoteStore(cfg).assets()[0]
    assert record["created_at"] == asset["created_at"]
    refresh_notes(cfg, [])
    assert NoteStore(cfg).assets()[0]["asset_id"] == asset["asset_id"]


@pytest.mark.parametrize("change", ["duplicate", "nested", "missing-tags"])
def test_invalid_templates_rejected(cfg, asset, monkeypatch, change):
    text = find_note(cfg, asset).read_text(encoding="utf-8")
    template = resource("note.md")
    if change == "duplicate":
        template = template.replace("bytes:", "sha256:")
    elif change == "nested":
        template = template.replace("bytes:", "release:\n  bytes:")
    else:
        template = template.replace("tags:\n", "")
    monkeypatch.setattr(layout, "resource", lambda _: template)
    with pytest.raises(AppError):
        format_existing_note(text)
    assert find_note(cfg, asset).read_text(encoding="utf-8") == text


def test_nested_user_properties_rejected_without_losing_values(cfg, asset):
    data, body = split_note(find_note(cfg, asset).read_text(encoding="utf-8"))
    data["custom"] = CommentedMap({"child": "value"})
    with pytest.raises(AppError, match="flat"):
        format_existing_note(serialize(data, body))

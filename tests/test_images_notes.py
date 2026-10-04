from __future__ import annotations

import pytest
from PIL import Image

from tkn_azure_blob_note.catalog import Catalog, Operation
from tkn_azure_blob_note.config import load_config
from tkn_azure_blob_note.errors import AppError, ConflictError
from tkn_azure_blob_note.images import build_images, import_images
from tkn_azure_blob_note.io import sha256
from tkn_azure_blob_note.notes import find_note, refresh_notes
from tkn_azure_blob_note.sync import verify


def snapshot(root):
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


def test_dry_run_no_files(cfg, source, tmp_path):
    before = snapshot(tmp_path)
    with Operation(cfg, "import", True) as operation:
        result = import_images(cfg, [source], operation)
    assert result[0]["conversion"] == "webp"
    assert snapshot(tmp_path) == before


def test_original_preserved_idempotent(cfg, source, asset):
    catalog = Catalog(cfg)
    original = cfg.data_root / asset["source"]["path"]
    assert original.read_bytes() == source.read_bytes()
    assert catalog.release(asset).suffix == ".webp"
    assert sha256(catalog.release(asset)) == asset["release"]["sha256"]
    assert all(item["status"] == "valid" for item in verify(cfg))
    note = find_note(cfg, asset)
    before = note.read_bytes()
    with Operation(cfg, "import", False) as operation:
        result = import_images(cfg, [source], operation)
    assert result[0]["status"] == "unchanged"
    assert len(catalog.assets()) == 1
    assert note.read_bytes() == before


def test_user_fields_comments_body_and_renamed_note(cfg, asset):
    note = find_note(cfg, asset)
    text = note.read_text(encoding="utf-8")
    text = text.replace("description: ''", 'description: "My annotation" # keep comment')
    text = text.replace("tags: []", 'tags: [personal]\ncustomRelation: "[[Related note]]"')
    text += "\n## User notes\nDo not change this.\n"
    note.write_text(text, encoding="utf-8")
    renamed = note.with_name("human-readable-name.md")
    note.rename(renamed)
    assert refresh_notes(cfg, [asset["asset_id"]])[0]["status"] in {"updated", "unchanged"}
    updated = renamed.read_text(encoding="utf-8")
    assert "# keep comment" in updated
    assert 'customRelation: "[[Related note]]"' in updated
    assert "Do not change this." in updated
    assert not note.exists()
    record = Catalog(cfg).assets()[0]
    assert record["note_path"] == "human-readable-name.md"
    assert record["relative_path"] == asset["relative_path"]


def test_title_change_does_not_rename_blob(cfg, asset):
    note = find_note(cfg, asset)
    note.write_text(
        note.read_text().replace("title: example", "title: A different title"), encoding="utf-8"
    )
    refresh_notes(cfg, [])
    assert Catalog(cfg).assets()[0]["relative_path"] == "example.webp"


def test_build_retains_previous_bytes(cfg, source, asset, tmp_path):
    old = Catalog(cfg).release(asset).read_bytes()
    changed = load_config(
        home=tmp_path / "settings",
        cwd=tmp_path,
        overrides={
            "data_root": str(cfg.data_root),
            "state_root": str(cfg.state_root),
            "conversion": {"quality": 20},
        },
    )
    with Operation(changed, "build", False) as operation:
        result = build_images(changed, [], operation)
    assert result[0]["status"] == "updated"
    assert list((cfg.data_root / "history").rglob("*.webp"))[0].read_bytes() == old
    assert source.exists()


def test_conversion_disabled(cfg, source):
    cfg.conversion["enabled"] = False
    with Operation(cfg, "import", False) as operation:
        import_images(cfg, [source], operation)
    asset = Catalog(cfg).assets()[0]
    assert Catalog(cfg).release(asset).read_bytes() == source.read_bytes()


def test_animation_passes_through(cfg, source):
    first = Image.new("RGB", (8, 8), "red")
    second = Image.new("RGB", (8, 8), "blue")
    first.save(source, save_all=True, append_images=[second], duration=100, loop=0)
    with Operation(cfg, "import", False) as operation:
        import_images(cfg, [source], operation)
    asset = Catalog(cfg).assets()[0]
    assert asset["relative_path"] == "example.png"
    assert Catalog(cfg).release(asset).read_bytes() == source.read_bytes()


def test_colliding_inputs_preflight(cfg, source):
    sibling = source.with_suffix(".jpg")
    Image.new("RGB", (8, 8), "red").save(sibling)
    with pytest.raises(ConflictError):
        with Operation(cfg, "import", False) as operation:
            import_images(cfg, [source.parent], operation)
    assert not (cfg.data_root / "releases").exists()
    assert source.exists()


def test_invalid_note_schema_preserved(cfg, asset):
    path = find_note(cfg, asset)
    content = path.read_text().replace("schemaVersion: 1.0.0", "schemaVersion: 2.0.0")
    path.write_text(content, encoding="utf-8")
    with pytest.raises(AppError):
        refresh_notes(cfg, [])
    assert path.read_text() == content


def test_unknown_existing_note_protected(cfg, source):
    note = cfg.notes_root / "example.webp.md"
    note.parent.mkdir(parents=True)
    note.write_text("# My unrelated note", encoding="utf-8")
    with pytest.raises(ConflictError):
        with Operation(cfg, "import", False) as operation:
            import_images(cfg, [source], operation)
    assert note.read_text() == "# My unrelated note"

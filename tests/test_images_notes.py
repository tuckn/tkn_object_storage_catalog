from __future__ import annotations

import pytest
from PIL import Image

from tkn_objstorage_imgcatalog.assets import NoteStore, Operation
from tkn_objstorage_imgcatalog.config import load_config
from tkn_objstorage_imgcatalog.errors import AppError, ConflictError
from tkn_objstorage_imgcatalog.images import build_images, import_images
from tkn_objstorage_imgcatalog.io import sha256
from tkn_objstorage_imgcatalog.notes import find_note, refresh_notes
from tkn_objstorage_imgcatalog.sync import verify


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
    store = NoteStore(cfg)
    original = cfg.data_root / asset["source"]["path"]
    assert original.read_bytes() == source.read_bytes()
    assert store.release(asset).suffix == ".webp"
    assert sha256(store.release(asset)) == asset["release"]["sha256"]
    assert all(item["status"] == "valid" for item in verify(cfg))
    note = find_note(cfg, asset)
    before = note.read_bytes()
    with Operation(cfg, "import", False) as operation:
        result = import_images(cfg, [source], operation)
    assert result[0]["status"] == "unchanged"
    assert len(store.assets()) == 1
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
    record = NoteStore(cfg).assets()[0]
    assert record["note_path"] == "human-readable-name.md"
    assert record["relative_path"] == asset["relative_path"]


def test_title_change_does_not_rename_blob(cfg, asset):
    note = find_note(cfg, asset)
    note.write_text(
        note.read_text().replace("title: example", "title: A different title"), encoding="utf-8"
    )
    refresh_notes(cfg, [])
    assert NoteStore(cfg).assets()[0]["relative_path"] == "example.webp"


def test_build_replaces_release_without_history(cfg, source, asset, tmp_path):
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
    assert not (cfg.data_root / "history").exists()
    assert verify(changed)[0]["status"] == "valid"
    assert source.exists()


def test_conversion_disabled(cfg, source):
    cfg.conversion["enabled"] = False
    with Operation(cfg, "import", False) as operation:
        import_images(cfg, [source], operation)
    asset = NoteStore(cfg).assets()[0]
    assert NoteStore(cfg).release(asset).read_bytes() == source.read_bytes()


def test_animation_passes_through(cfg, source):
    first = Image.new("RGB", (8, 8), "red")
    second = Image.new("RGB", (8, 8), "blue")
    first.save(source, save_all=True, append_images=[second], duration=100, loop=0)
    with Operation(cfg, "import", False) as operation:
        import_images(cfg, [source], operation)
    asset = NoteStore(cfg).assets()[0]
    assert asset["relative_path"] == "example.png"
    assert NoteStore(cfg).release(asset).read_bytes() == source.read_bytes()


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
    content = path.read_text().replace("schemaVersion: 3.0.0", "schemaVersion: 99.0.0")
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


@pytest.mark.parametrize("move_vault", [False, True])
def test_vault_relative_preview_survives_note_and_vault_moves(cfg, source, tmp_path, move_vault):
    from urllib.parse import unquote

    from tkn_objstorage_imgcatalog.notes import split_note

    with Operation(cfg, "import", False) as operation:
        import_images(cfg, [source], operation, name="goods/生成 画像.png")
    record = NoteStore(cfg).assets()[0]
    note = find_note(cfg, record)
    moved_note = cfg.notes_root / "organized" / "nested" / "renamed.md"
    moved_note.parent.mkdir(parents=True)
    note.rename(moved_note)
    if move_vault:
        destination = tmp_path / "moved-vault"
        cfg.data_root.rename(destination)
        cfg = load_config(
            home=tmp_path / "empty",
            cwd=tmp_path,
            overrides={"data_root": str(destination), "state_root": str(cfg.state_root)},
        )
        moved_note = cfg.notes_root / "organized" / "nested" / "renamed.md"
    before = snapshot(tmp_path)
    refresh_notes(cfg, [], dry_run=True)
    assert snapshot(tmp_path) == before
    refresh_notes(cfg, [])
    data, body = split_note(moved_note.read_text(encoding="utf-8"))
    assert data["cover"] == "releases/goods/生成 画像.webp"
    assert (cfg.data_root / data["cover"]).is_file()
    preview = body.split("![Image](", 1)[1].split(")", 1)[0]
    assert not preview.startswith("file:")
    assert (moved_note.parent / unquote(preview)).resolve() == cfg.data_root / data["cover"]
    assert NoteStore(cfg).assets()[0]["asset_id"] == record["asset_id"]
    assert verify(cfg)[0]["status"] == "valid"
    assert (cfg.notes_root / "images.base").is_file()
    assert not (cfg.data_root / ".obsidian").exists()

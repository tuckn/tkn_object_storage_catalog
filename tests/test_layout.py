from __future__ import annotations

import json

import pytest
from ruamel.yaml import YAML

from tkn_objstorage_imgcatalog import cli
from tkn_objstorage_imgcatalog.assets import NoteStore, Operation, make_record
from tkn_objstorage_imgcatalog.config import resource
from tkn_objstorage_imgcatalog.errors import AppError
from tkn_objstorage_imgcatalog.images import import_images
from tkn_objstorage_imgcatalog.notes import refresh_notes

FOLDERS = ("1_staging", "2_originals", "3_releases", "4_notes")
COMMANDS = (
    ["import"],
    ["upload"],
    ["build"],
    ["push"],
    ["pull"],
    ["notes", "refresh"],
    ["recover"],
)


def snapshot(root):
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


def isolated_cli(cfg, monkeypatch, blobs):
    monkeypatch.setattr(cli, "load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(cli, "open_store", lambda *a: blobs)


@pytest.mark.parametrize("command", COMMANDS)
def test_first_mutating_command_initializes_empty_library(cfg, monkeypatch, blobs, capsys, command):
    isolated_cli(cfg, monkeypatch, blobs)
    assert cli.main([*command, "--source", cfg.source_id]) == 0
    assert json.loads(capsys.readouterr().out)["source_id"] == cfg.source_id
    assert all((cfg.data_root / name).is_dir() for name in FOLDERS)
    base = cfg.notes_root / "index.base"
    assert base.read_text(encoding="utf-8") == resource("index.base")
    assert [v["type"] for v in YAML(typ="safe").load(base.read_text())["views"]] == [
        "cards",
        "table",
    ]
    assert not (cfg.notes_root / "images.base").exists()
    assert not list((cfg.data_root / "2_originals").rglob("*"))
    assert NoteStore(cfg).assets() == []


@pytest.mark.parametrize("command", COMMANDS)
def test_dry_run_does_not_initialize(cfg, monkeypatch, blobs, capsys, command):
    isolated_cli(cfg, monkeypatch, blobs)
    assert cli.main([*command, "--source", cfg.source_id, "--dry-run"]) == 0
    capsys.readouterr()
    assert not cfg.data_root.exists()
    assert not cfg.state_root.exists()


@pytest.mark.parametrize("command", (["status"], ["verify"], ["config", "list", "--json"]))
def test_readonly_commands_do_not_initialize(cfg, monkeypatch, blobs, capsys, command):
    isolated_cli(cfg, monkeypatch, blobs)
    assert cli.main([*command, "--source", cfg.source_id]) == 0
    capsys.readouterr()
    assert not cfg.data_root.exists()
    assert not cfg.state_root.exists()


@pytest.mark.parametrize("existing_root", [False, True])
def test_first_pull_creates_layout_and_downloads_without_original_files(
    cfg, monkeypatch, blobs, capsys, existing_root
):
    if existing_root:
        cfg.data_root.mkdir(parents=True)
    isolated_cli(cfg, monkeypatch, blobs)
    blobs.put("nested/photo.webp", b"exact remote bytes")
    assert cli.main(["pull", "--source", cfg.source_id]) == 0
    capsys.readouterr()
    assert all((cfg.data_root / name).is_dir() for name in FOLDERS)
    assert (cfg.notes_root / "index.base").is_file()
    assert (cfg.data_root / "3_releases/nested/photo.webp").read_bytes() == b"exact remote bytes"
    assert (cfg.notes_root / "nested/photo.webp.md").is_file()
    assert not list((cfg.data_root / "2_originals").rglob("*"))
    assert NoteStore(cfg).assets()[0]["source"] is None


def test_unrelated_vault_files_and_pending_inputs_allow_initialization(cfg, source):
    for name, content in (
        (".obsidian/app.json", b"{}"),
        ("4_notes/personal.md", b"# Personal note"),
        ("4_notes/images.base", b"my existing view"),
        ("1_staging/photo.png", source.read_bytes()),
        ("3_releases/readme.txt", b"ordinary file"),
    ):
        path = cfg.data_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    before = snapshot(cfg.data_root)
    with Operation(cfg, "import", False) as operation:
        assert len(import_images(cfg, [], operation)) == 1
    assert all((cfg.data_root / name).is_dir() for name in FOLDERS)
    assert (cfg.notes_root / "index.base").is_file()
    after = snapshot(cfg.data_root)
    assert all(after[name] == value for name, value in before.items())


def test_empty_library_preserves_edited_reference_on_repeat(cfg):
    base = cfg.notes_root / "index.base"
    base.parent.mkdir(parents=True)
    base.write_bytes(b"custom reference, even without valid YAML")
    before = (base.read_bytes(), base.stat().st_mtime_ns)
    for _ in range(2):
        with Operation(cfg, "import", False):
            pass
    assert all((cfg.data_root / name).is_dir() for name in FOLDERS)
    assert (base.read_bytes(), base.stat().st_mtime_ns) == before


@pytest.mark.parametrize("folder", ["2_originals", "3_releases", "4_notes"])
def test_existing_library_is_not_initialized(cfg, folder):
    if folder == "4_notes":
        record = make_record("photo.webp", "test", source=None, digest="a" * 64, size=1)
        NoteStore(cfg).save(record)
    else:
        path = cfg.data_root / folder / "nested/PHOTO.PNG"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"existing image")
    before = snapshot(cfg.data_root)
    with Operation(cfg, "build", False):
        pass
    assert not (cfg.notes_root / "index.base").exists()
    assert not (cfg.data_root / "1_staging").exists()
    after = snapshot(cfg.data_root)
    assert all(after[name] == value for name, value in before.items())


@pytest.mark.parametrize("keep_base", [False, True])
def test_reference_does_not_affect_existing_image_processing(cfg, asset, monkeypatch, keep_base):
    import tkn_objstorage_imgcatalog.assets as assets

    base = cfg.notes_root / "index.base"
    if keep_base:
        base.write_bytes(b"invalid YAML [")
    else:
        base.unlink()
    before = snapshot(cfg.notes_root)
    monkeypatch.setattr(assets, "resource", lambda *a: pytest.fail("Reference was read"))
    with Operation(cfg, "notes", False):
        assert refresh_notes(cfg, [asset["asset_id"]])[0]["status"] == "unchanged"
    assert snapshot(cfg.notes_root) == before


@pytest.mark.parametrize("name", FOLDERS)
def test_initialization_does_not_replace_file_at_folder_path(cfg, name):
    cfg.data_root.mkdir(parents=True)
    path = cfg.data_root / name
    path.write_bytes(b"user file")
    with pytest.raises(AppError, match="occupied by a file"):
        with Operation(cfg, "import", False):
            pass
    assert path.read_bytes() == b"user file"
    assert not (cfg.notes_root / "index.base").exists()
    run = json.loads(next((cfg.state_root / "runs").glob("*.json")).read_text())
    assert run["status"] == "failed"
    assert run["error_type"] == "AppError"


def test_initialization_does_not_replace_invalid_image_note(cfg):
    cfg.notes_root.mkdir(parents=True)
    note = cfg.notes_root / "invalid.md"
    note.write_text("---\nassetId: invalid\n---\n", encoding="utf-8")
    before = snapshot(cfg.notes_root)
    with pytest.raises(AppError, match="Unsupported note schema"):
        with Operation(cfg, "import", False):
            pass
    assert snapshot(cfg.notes_root) == before
    assert not (cfg.data_root / "2_originals").exists()


@pytest.mark.parametrize("old", ["staging", "originals", "releases", "notes"])
@pytest.mark.parametrize("command", (["status"], ["verify"], ["pull"]))
def test_old_folder_data_is_not_silently_ignored(cfg, monkeypatch, blobs, capsys, old, command):
    path = cfg.data_root / old / "existing.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing data")
    isolated_cli(cfg, monkeypatch, blobs)
    before = snapshot(cfg.data_root)
    assert cli.main([*command, "--source", cfg.source_id]) == 2
    assert "Old storage layout" in capsys.readouterr().err
    assert snapshot(cfg.data_root) == before
    assert not cfg.state_root.exists()

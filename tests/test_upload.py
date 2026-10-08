from __future__ import annotations

import json
from copy import deepcopy
from io import StringIO

import pytest
from azure.core.exceptions import AzureError
from PIL import Image

from tkn_objstorage_imgcatalog import cli
from tkn_objstorage_imgcatalog.assets import NoteStore


@pytest.fixture
def run_upload(cfg, monkeypatch, blobs, capsys, tmp_path):
    original_load = cli.load_config

    def isolated_load(path, overrides, *, source):
        values = deepcopy(cfg.source_values)
        for key, value in overrides.items():
            if isinstance(value, dict):
                values[key].update(value)
            else:
                values[key] = value
        return original_load(
            overrides=values, source=source, home=tmp_path / "settings", cwd=tmp_path
        )

    monkeypatch.setattr(cli, "load_config", isolated_load)
    monkeypatch.setattr(cli, "open_store", lambda *a: blobs)
    monkeypatch.setattr(cli.sys, "stdin", StringIO())

    def run(*args):
        code = cli.main(["upload", "--source", cfg.source_id, *map(str, args)])
        output = capsys.readouterr()
        return code, json.loads(output.out), output.err

    return run


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_upload_only_current_input_and_reuses_unchanged(cfg, asset, tmp_path, blobs, run_upload):
    source = tmp_path / "another.png"
    Image.new("RGB", (8, 8), "blue").save(source)
    code, result, _ = run_upload(source, "--name", "travel/photo.webp")
    assert code == 0
    assert result["command"] == "upload"
    assert result["result"]["upload"][0]["path"] == "travel/photo.webp"
    assert set(blobs.values) == {"travel/photo.webp"}
    assert NoteStore(cfg).baseline(asset) is None
    code, result, _ = run_upload(source, "--name", "travel/photo.webp")
    assert code == 0
    assert result["result"]["import"][0]["status"] == "unchanged"
    assert result["result"]["upload"][0]["status"] == "unchanged"
    assert blobs.writes == 1


def test_upload_empty_staging_never_pushes_existing_assets(asset, monkeypatch, run_upload):
    monkeypatch.setattr(cli, "open_store", lambda *a: pytest.fail("Unexpected connection"))
    code, result, _ = run_upload()
    assert code == 0
    assert result["result"] == {"import": [], "upload": []}


def test_upload_staging_multiple_no_conversion(cfg, blobs, run_upload):
    staging = cfg.data_root / "1_staging"
    staging.mkdir(parents=True)
    for name in ("one", "two"):
        Image.new("RGB", (8, 8), "red").save(staging / f"{name}.png")
    code, result, _ = run_upload("--no-convert")
    assert code == 0
    assert set(blobs.values) == {"one.png", "two.png"}
    for name in blobs.values:
        assert blobs.values[name][0] == (staging / name).read_bytes()
    assert len(result["result"]["import"]) == 2


def test_upload_preview_no_writes_conversion_or_network(
    cfg, source, tmp_path, monkeypatch, run_upload
):
    import tkn_objstorage_imgcatalog.images as images

    cfg.azure["prefix"] = "gallery"
    before = snapshot(tmp_path)
    monkeypatch.setattr(cli, "open_store", lambda *a: pytest.fail("Unexpected connection"))
    monkeypatch.setattr(images, "render_image", lambda *a: pytest.fail("Unexpected conversion"))
    code, result, _ = run_upload(source, "--name", "travel/photo.webp", "--dry-run")
    assert code == 0
    assert result["run_id"] is None
    planned = result["result"]["upload"][0]
    assert planned["object_key"] == "gallery/travel/photo.webp"
    assert planned["status"] == "pending_remote_check"
    assert snapshot(tmp_path) == before


def test_upload_failure_retains_import_and_retry_succeeds(
    cfg, source, monkeypatch, blobs, run_upload
):
    closed = []
    monkeypatch.setattr(blobs, "close", lambda: closed.append(True))
    original = blobs.upload
    monkeypatch.setattr(
        blobs, "upload", lambda *a: (_ for _ in ()).throw(AzureError("simulated failure"))
    )
    code, _, error = run_upload(source)
    assert code == 3
    assert "local results are retained" in error
    record = NoteStore(cfg).assets()[0]
    assert NoteStore(cfg).release(record).exists()
    assert NoteStore(cfg).baseline(record) is None
    assert closed == [True]
    monkeypatch.setattr(blobs, "upload", original)
    code, result, _ = run_upload(source)
    assert code == 0
    assert result["result"]["import"][0]["status"] == "unchanged"
    assert NoteStore(cfg).baseline(record) is not None


def test_upload_remote_conflict_preserves_remote(cfg, source, blobs, run_upload):
    blobs.put("example.webp", b"other content")
    code, result, _ = run_upload(source)
    assert code == 2
    assert "Remote differs" in result["error"]
    assert blobs.values["example.webp"][0] == b"other content"
    assert len(NoteStore(cfg).assets()) == 1


def test_upload_requires_public_confirmation(source, blobs, run_upload):
    blobs.public = True
    code, result, _ = run_upload(source)
    assert code == 2
    assert "confirmation" in result["error"]
    assert blobs.writes == 0
    assert run_upload(source, "--yes")[0] == 0
    assert blobs.writes == 1


@pytest.mark.parametrize("answer, expected", [("y", 0), ("n", 2)])
def test_upload_interactive_confirmation(source, blobs, monkeypatch, run_upload, answer, expected):
    blobs.public = True
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: answer)
    assert run_upload(source)[0] == expected
    assert blobs.writes == (1 if expected == 0 else 0)


def test_upload_import_conflict_does_not_connect(asset, source, monkeypatch, run_upload):
    Image.new("RGB", (8, 8), "green").save(source)
    monkeypatch.setattr(cli, "open_store", lambda *a: pytest.fail("Unexpected connection"))
    assert run_upload(source)[0] == 2

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tkn_objstorage_imgcatalog.cli import main
from tkn_objstorage_imgcatalog.config import init_config, load_config, resource
from tkn_objstorage_imgcatalog.errors import AppError, ConflictError


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_five_layers_and_origin(tmp_path):
    home = tmp_path / "user"
    cwd = tmp_path / "working"
    write(
        home / "config.yaml",
        'schema_version: "1.0.0"\nconversion:\n  quality: 60\n  lossless: true\n',
    )
    write(cwd / ".tkn/config.yaml", 'schema_version: "1.0.0"\nconversion:\n  quality: 70\n')
    extra = tmp_path / "chosen.yaml"
    write(extra, 'schema_version: "1.0.9"\nconversion:\n  quality: 80\n')
    config = load_config(extra, {"conversion": {"quality": 90}}, home=home, cwd=cwd)
    assert config.conversion["quality"] == 90
    assert config.conversion["lossless"] is True
    assert config.origins["sources.images.conversion.quality"] == "CLI"
    assert config.origins["sources.images.conversion.lossless"] == str(home / "config.yaml")
    assert len(config.loaded_sources) == 4
    assert config.loaded_sources[-1]["schema_version"] == "1.0.9"


@pytest.mark.parametrize(
    "content",
    [
        "conversion: {}",
        "schema_version: 1\n",
        'schema_version: "5.0.0"\n',
        'schema_version: "1.1.0"\n',
        'schema_version: "1.0"\n',
        'schema_version: "1.0.0"\nconversion:\n  mystery: 1\n',
        'schema_version: "1.0.0"\nconversion:\n  enabled: 1\n',
        'schema_version: "1.0.0"\nconversion:\n  quality: 101\n',
        'schema_version: "1.0.0"\nazure:\n  prefix: ../outside\n',
        'schema_version: "1.0.0"\nazure:\n  account_url: https://example.com?sig=secret\n',
        'schema_version: "1.0.0"\nnotes_root: ""\n',
        'schema_version: "1.0.0"\nconversion: null\n',
        'schema_version: "1.0.0"\nconversion:\n  enabled: true\n  enabled: false\n',
    ],
)
def test_rejects_invalid_source_before_merge(tmp_path, content):
    path = tmp_path / "config.yaml"
    write(path, content)
    with pytest.raises(AppError):
        load_config(path, {"conversion": {"quality": 82}}, home=tmp_path / "empty", cwd=tmp_path)


def test_init_protection_and_backup(tmp_path):
    path = tmp_path / "new" / "config.yaml"
    assert init_config(path, dry_run=True)["status"] == "created"
    assert not path.exists()
    assert init_config(path)["status"] == "created"
    assert init_config(path)["status"] == "unchanged"
    assert 'schema_version: "4.0.0"' in path.read_text()
    path.write_text("# Edited\n" + path.read_text(), encoding="utf-8")
    with pytest.raises(ConflictError):
        init_config(path)
    old = path.read_bytes()
    result = init_config(path, force=True)
    assert Path(result["backup"]).read_bytes() == old


def test_missing_explicit_and_overlapping_roots(tmp_path):
    with pytest.raises(AppError):
        load_config(tmp_path / "missing", home=tmp_path / "none", cwd=tmp_path)
    with pytest.raises(AppError, match="separate"):
        load_config(
            overrides={"data_root": str(tmp_path), "state_root": str(tmp_path / "state")},
            home=tmp_path / "none",
            cwd=tmp_path,
        )


def test_config_and_help_readonly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    assert main(["config", "list", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert "notes_root" not in result["config"]["sources"]["my-obj-storage-1"]
    assert not (tmp_path / "home").exists()
    for option in ("--help", "--version"):
        with pytest.raises(SystemExit) as stop:
            main([option])
        assert stop.value.code == 0
        capsys.readouterr()
    assert not (tmp_path / "home").exists()


def test_installed_entrypoint_outside_repository(tmp_path):
    completed = subprocess.run(
        [sys.executable, "-m", "tkn_objstorage_imgcatalog", "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "tkn-objstorage-imgcatalog 0.9.0" in completed.stdout


def test_cli_import_json_and_quiet(cfg, source, capsys):
    args = [
        "--source",
        "my-obj-storage-1",
        "--data-root",
        str(cfg.data_root),
        "import",
        str(source),
        "--state-root",
        str(cfg.state_root),
        "--quiet",
    ]
    assert main(args) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["result"][0]["status"] == "created"
    assert output.err == ""


def test_resource_available():
    assert "$title" in resource("note.md")


@pytest.mark.parametrize("version", ["1.0.0", "2.0.0", "3.2.0", "4.0.0"])
@pytest.mark.parametrize("notes", [None, "data/notes", "external-vault"])
def test_removed_notes_setting_rejected_without_writes(tmp_path, version, notes):
    settings = {"data_root": "data", "notes_root": notes}
    value = {"schema_version": version}
    value.update(settings if version == "1.0.0" else {"sources": {"images": settings}})
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(value), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(AppError, match="notes_root is no longer supported"):
        load_config(path, home=tmp_path / "empty", cwd=tmp_path)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_removed_notes_override_rejected(tmp_path):
    with pytest.raises(AppError, match="notes_root is no longer supported"):
        load_config(overrides={"notes_root": None}, home=tmp_path / "empty", cwd=tmp_path)
    assert not list(tmp_path.iterdir())


def test_removed_notes_cli_option_rejected_before_config(monkeypatch, capsys):
    import tkn_objstorage_imgcatalog.cli as cli

    monkeypatch.setattr(cli, "load_config", lambda *a, **k: pytest.fail("Config read"))
    with pytest.raises(SystemExit) as stop:
        main(["config", "list", "--notes-root", "external-vault"])
    assert stop.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_notes_follow_resolved_data_root_override(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        json.dumps({"schema_version": "4.0.0", "sources": {"images": {"data_root": "old"}}}),
        encoding="utf-8",
    )
    cfg = load_config(path, {"data_root": "new-vault"}, home=tmp_path / "empty", cwd=tmp_path)
    assert cfg.notes_root == tmp_path / "new-vault" / "notes"
    assert "notes_root" not in cfg.source_values
    assert not cfg.data_root.exists()

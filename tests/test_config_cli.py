from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tkn_azure_blob_note.cli import main
from tkn_azure_blob_note.config import init_config, load_config, resource
from tkn_azure_blob_note.errors import AppError, ConflictError


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
        'schema_version: "3.0.0"\n',
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
    assert 'schema_version: "2.0.0"' in path.read_text()
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
    assert result["config"]["sources"]["images"]["notes_root"].endswith("notes")
    assert not (tmp_path / "home").exists()
    for option in ("--help", "--version"):
        with pytest.raises(SystemExit) as stop:
            main([option])
        assert stop.value.code == 0
        capsys.readouterr()
    assert not (tmp_path / "home").exists()


def test_installed_entrypoint_outside_repository(tmp_path):
    completed = subprocess.run(
        [sys.executable, "-m", "tkn_azure_blob_note", "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "tkn-azure-blob-note 0.2.0" in completed.stdout


def test_cli_import_json_and_quiet(cfg, source, capsys):
    args = [
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

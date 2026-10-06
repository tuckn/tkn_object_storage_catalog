from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from ruamel.yaml import YAML

import tkn_objstorage_imgcatalog.cli as cli
from tkn_objstorage_imgcatalog.catalog import Catalog, Operation
from tkn_objstorage_imgcatalog.config import load_config, resource
from tkn_objstorage_imgcatalog.errors import AppError
from tkn_objstorage_imgcatalog.images import import_images
from tkn_objstorage_imgcatalog.sync import push


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)


def write_config(tmp_path, sources, name="config.yaml"):
    path = tmp_path / name
    with path.open("w", encoding="utf-8") as stream:
        YAML().dump({"schema_version": "2.0.0", "sources": sources}, stream)
    return path


def pair():
    return {
        "public-images": {
            "azure": {
                "account_url": "https://example.blob.core.windows.net/",
                "container": "images",
            },
            "delivery": {"url_base": "https://images.example.com/"},
            "conversion": {"quality": 65},
        },
        "private-images": {
            "azure": {
                "account_url": "https://example.blob.core.windows.net",
                "container": "private-images",
            },
            "conversion": {"enabled": False},
        },
    }


def snapshot(root):
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_named_defaults_and_readonly_report(tmp_path):
    config = load_config()
    assert config.source_id == "my-obj-storage-1"
    assert config.data_root == tmp_path / "home/.tkn/objstorage-imgcatalog/data/my-obj-storage-1"
    assert config.state_root == tmp_path / "home/.tkn/objstorage-imgcatalog/state/my-obj-storage-1"
    assert config.notes_root == config.data_root / "notes"
    report = config.report()
    assert report["config"]["schema_version"] == "3.1.0"
    assert report["effective_schema_version"] == "3.1.0"
    assert report["selected_source"] == "my-obj-storage-1"
    assert not (tmp_path / "home").exists()


def test_two_sources_defaults_settings_and_selection(tmp_path):
    path = write_config(tmp_path, pair())
    before = snapshot(tmp_path)
    config = load_config(path)
    assert config.source_id is None
    assert set(config.sources) == {"public-images", "private-images"}
    public = config.select_source("public-images")
    private = config.select_source("private-images")
    assert public.data_root.name == "public-images"
    assert private.data_root.name == "private-images"
    assert public.state_root.name == "public-images"
    assert public.notes_root == public.data_root / "notes"
    assert public.azure["account_url"] == "https://example.blob.core.windows.net"
    assert public.delivery["url_base"] == "https://images.example.com"
    assert private.delivery["url_base"] is None
    assert public.conversion["quality"] == 65
    assert private.conversion["quality"] == 82
    assert private.conversion["enabled"] is False
    with pytest.raises(AppError, match="Specify --source"):
        config.select_source()
    with pytest.raises(AppError, match="Unknown source"):
        config.select_source("missing")
    assert snapshot(tmp_path) == before
    assert not (tmp_path / "home").exists()


def test_sources_mapping_replaces_and_cli_only_overrides_selected_source(tmp_path):
    home = tmp_path / "home/.tkn/azure_blob_note"
    home.mkdir(parents=True)
    write_config(home, {"old": {"conversion": {"lossless": True}}})
    local = tmp_path / ".tkn"
    local.mkdir()
    write_config(
        local,
        {
            "public-images": {"conversion": {"quality": 70}},
            "private-images": {},
        },
    )
    explicit = write_config(tmp_path, pair())
    config = load_config(
        explicit,
        {"conversion": {"quality": 90}, "data_root": "chosen-data"},
        source="private-images",
    )
    assert "old" not in config.sources
    assert config.source_id == "private-images"
    assert config.conversion["quality"] == 90
    assert config.data_root == tmp_path / "chosen-data"
    assert config.notes_root == config.data_root / "notes"
    assert config.select_source("public-images").conversion["quality"] == 65
    assert config.origins["sources.private-images.conversion.quality"] == "CLI"
    assert config.origins["sources.public-images.conversion.quality"] == str(explicit)
    assert config.origins["sources.public-images.conversion.lossless"] == "built-in"
    assert config.origins["schema_version"] == "built-in"
    assert len(config.loaded_sources) == 4


@pytest.mark.parametrize(
    "source_id", ["../outside", "nested/id", "con", "NUL", "Images", "", "a.b", 7]
)
def test_invalid_source_ids(tmp_path, source_id):
    path = write_config(tmp_path, {source_id: {}})
    with pytest.raises(AppError):
        load_config(path)
    assert not (tmp_path / "home").exists()


@pytest.mark.parametrize(
    "settings",
    [
        None,
        [],
        {"extra": True},
        {"azure": None},
        {"conversion": []},
        {"conversion": {"enabled": 1}},
        {"conversion": {"quality": True}},
        {"conversion": {"quality": 101}},
        {"conversion": {"format": "png"}},
        {"delivery": {"url_base": "https://example.com?sig=secret"}},
        {"azure": {"account_url": "https://example.com/container"}},
        {"azure": {"prefix": "../other"}},
        {"azure": {"container": "BAD"}},
        {"data_root": ""},
        {"state_root": True},
        {"notes_root": ""},
    ],
)
def test_invalid_source_settings_cannot_be_hidden_by_higher_layer(tmp_path, settings):
    home = tmp_path / "home/.tkn/azure_blob_note"
    home.mkdir(parents=True)
    path = write_config(home, {"images": settings})
    explicit = write_config(tmp_path, {"images": {}})
    before = path.read_bytes()
    with pytest.raises(AppError):
        load_config(explicit)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "contents",
    [
        'schema_version: "2.0.0"\nsources: []\n',
        'schema_version: "2.0.0"\nconversion: {}\n',
        'schema_version: "2.0.0"\ndelivery: {}\n',
        'schema_version: "2.0.0"\nazure: {}\n',
        'schema_version: "2.0.0"\nsources:\n  images: {}\n  images: {}\n',
        'schema_version: "2.1.0"\nsources: {}\n',
    ],
)
def test_invalid_top_level_and_duplicate_keys(tmp_path, contents):
    path = tmp_path / "invalid.yaml"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(AppError):
        load_config(path)


def test_one_container_per_source_even_with_different_prefixes(tmp_path):
    sources = pair()
    sources["private-images"]["azure"].update(
        account_url="https://EXAMPLE.blob.core.windows.net",
        container="images",
        prefix="private",
    )
    with pytest.raises(AppError, match="one container"):
        load_config(write_config(tmp_path, sources))


def test_same_container_name_in_different_accounts_is_valid(tmp_path):
    sources = pair()
    sources["private-images"]["azure"].update(
        account_url="https://other.blob.core.windows.net",
        container="images",
    )
    assert len(load_config(write_config(tmp_path, sources)).sources) == 2


@pytest.mark.parametrize(
    "key, other_key, nested",
    [
        ("data_root", "data_root", False),
        ("state_root", "state_root", True),
        ("notes_root", "notes_root", False),
        ("data_root", "notes_root", False),
        ("notes_root", "state_root", True),
    ],
)
def test_sources_cannot_share_or_nest_managed_paths(tmp_path, key, other_key, nested):
    sources = pair()
    sources["public-images"][key] = str(tmp_path / "shared")
    sources["private-images"][other_key] = str(
        tmp_path / "shared" / "child" if nested else tmp_path / "shared"
    )
    with pytest.raises(AppError, match="overlaps"):
        load_config(write_config(tmp_path, sources))


def test_legacy_config_preserves_existing_library_and_baseline(cfg, asset, tmp_path):
    Catalog(cfg).set_baseline(asset, {"etag": "existing"})
    path = tmp_path / "legacy.yaml"
    legacy = deepcopy(cfg.source_values)
    legacy["schema_version"] = "1.0.9"
    with path.open("w", encoding="utf-8") as stream:
        YAML().dump(legacy, stream)
    before = snapshot(tmp_path)
    migrated = load_config(path)
    assert migrated.source_id == "images"
    assert migrated.data_root == cfg.data_root
    assert migrated.state_root == cfg.state_root
    assert Catalog(migrated).assets()[0]["asset_id"] == asset["asset_id"]
    assert Catalog(migrated).baseline(asset)["etag"] == "existing"
    assert migrated.loaded_sources[-1]["migrated"] is True
    assert snapshot(tmp_path) == before


def test_legacy_omitted_roots_keep_legacy_defaults(tmp_path):
    path = tmp_path / "legacy.yaml"
    path.write_text('schema_version: "1.0.0"\n', encoding="utf-8")
    config = load_config(path)
    assert config.data_root == tmp_path / "home/.tkn/azure_blob_note/data"
    assert config.state_root == tmp_path / "home/.tkn/azure_blob_note/state"
    assert config.notes_root == config.data_root / "notes"


def test_cli_reports_all_sources_and_requires_selection_before_any_work(
    tmp_path, monkeypatch, capsys
):
    path = write_config(tmp_path, pair())
    monkeypatch.setattr(cli, "open_store", lambda *_: pytest.fail("Unexpected Azure access"))
    before = snapshot(tmp_path)
    assert cli.main(["--config", str(path), "config", "list", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report["config"]["sources"]) == set(pair())
    assert report["selected_source"] is None
    for args in [
        ["import", "--dry-run"],
        ["build", "--dry-run"],
        ["push", "--dry-run"],
        ["pull", "--dry-run"],
        ["notes", "refresh", "--dry-run"],
        ["recover", "--dry-run"],
        ["status"],
        ["verify", "--remote"],
    ]:
        assert cli.main(["--config", str(path), *args]) == 2
        assert "Specify --source" in json.loads(capsys.readouterr().out)["error"]
    assert snapshot(tmp_path) == before
    assert not (tmp_path / "home").exists()


@pytest.mark.parametrize(
    "args, function",
    [
        (["import", "--dry-run"], "import_images"),
        (["build", "--dry-run"], "build_images"),
        (["push", "--dry-run"], "push"),
        (["pull", "--dry-run"], "pull"),
        (["notes", "refresh", "--dry-run"], "refresh_notes"),
        (["recover", "--dry-run"], "recover"),
        (["status"], "status"),
        (["verify", "--remote"], "verify"),
    ],
)
def test_every_command_routes_to_selected_source(
    tmp_path, monkeypatch, capsys, blobs, args, function
):
    path = write_config(tmp_path, pair())
    calls = []
    connections = []

    def capture(config, *args, **kwargs):
        calls.append(config.source_id)
        return []

    def adapter(config):
        connections.append(config.source_id)
        return blobs

    monkeypatch.setattr(cli, function, capture)
    monkeypatch.setattr(cli, "open_store", adapter)
    monkeypatch.setattr(blobs, "close", lambda: None, raising=False)
    assert cli.main(["--source", "private-images", "--config", str(path), *args]) == 0
    assert calls == ["private-images"]
    assert json.loads(capsys.readouterr().out)["source_id"] == "private-images"
    if args[0] in {"push", "pull", "verify"}:
        assert connections == ["private-images"]
    assert not (tmp_path / "home").exists()


def test_cli_import_uses_separate_libraries_conversion_and_delivery(tmp_path, source, capsys):
    sources = pair()
    for source_id, settings in sources.items():
        settings["data_root"] = str(tmp_path / ("p" if source_id == "public-images" else "q"))
        settings["state_root"] = str(tmp_path / ("ps" if source_id == "public-images" else "qs"))
    path = write_config(tmp_path, sources)
    for selected, expected in [
        ("public-images", "example.webp"),
        ("private-images", "example.png"),
    ]:
        assert cli.main(["--config", str(path), "import", str(source), "--source", selected]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["source_id"] == selected
        assert result["result"][0]["path"] == expected
    config = load_config(path)
    public = config.select_source("public-images")
    private = config.select_source("private-images")
    assert len(Catalog(public).assets()) == len(Catalog(private).assets()) == 1
    public_record, private_record = Catalog(public).assets()[0], Catalog(private).assets()[0]
    assert public_record["asset_id"] != private_record["asset_id"]
    assert (
        "https://images.example.com/example.webp"
        in (public.notes_root / public_record["note_path"]).read_text()
    )
    assert (
        "https://example.blob.core.windows.net/private-images/example.png"
        in (private.notes_root / private_record["note_path"]).read_text()
    )
    run = next((public.state_root / "runs").glob("*.json"))
    assert json.loads(run.read_text())["source_id"] == "public-images"


def test_same_release_name_and_sync_baselines_stay_separate(tmp_path, source, blobs):
    sources = pair()
    sources["private-images"]["conversion"]["enabled"] = True
    for source_id, settings in sources.items():
        settings["data_root"] = str(tmp_path / ("p" if source_id == "public-images" else "q"))
        settings["state_root"] = str(tmp_path / ("ps" if source_id == "public-images" else "qs"))
    config = load_config(write_config(tmp_path, sources))
    public = config.select_source("public-images")
    private = config.select_source("private-images")
    for selected in (public, private):
        with Operation(selected, "import", False) as operation:
            import_images(selected, [source], operation)
    first, second = Catalog(public).assets()[0], Catalog(private).assets()[0]
    assert first["relative_path"] == second["relative_path"] == "example.webp"
    # The same original and release name retain the same ID in separate catalogs.
    # Baselines must still be isolated by each source's library and Azure target.
    assert first["asset_id"] == second["asset_id"]
    with Operation(public, "push", False) as operation:
        push(public, [], operation, blobs, yes=True)
    assert Catalog(public).baseline(first) is not None
    assert Catalog(private).baseline(second) is None
    assert not (private.state_root / "sync").exists()


def test_empty_sources_can_be_listed_but_not_selected(tmp_path):
    config = load_config(write_config(tmp_path, {}))
    assert config.report()["config"]["sources"] == {}
    with pytest.raises(AppError, match="Specify --source"):
        config.select_source()


def test_template_has_custom_source_id_and_valid_second_source_example(tmp_path):
    example = resource("config.example.yaml")
    active = YAML(typ="safe").load(example)
    assert set(active["sources"]) == {"my-obj-storage-1"}
    enabled = example.replace("  # my-obj-storage-2:", "  my-obj-storage-2:")
    enabled = "\n".join(
        line.replace("  #   ", "    ", 1) if line.startswith("  #   ") else line
        for line in enabled.splitlines()
    )
    path = tmp_path / "two-sources.yaml"
    path.write_text(enabled, encoding="utf-8")
    config = load_config(path)
    assert set(config.sources) == {"my-obj-storage-1", "my-obj-storage-2"}
    assert config.source_id is None
    assert config.select_source("my-obj-storage-2").data_root.name == "my-obj-storage-2"

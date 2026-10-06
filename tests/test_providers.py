from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock
from uuid import NAMESPACE_URL, uuid5

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from ruamel.yaml import YAML

from tkn_objstorage_imgcatalog import cli
from tkn_objstorage_imgcatalog.assets import NoteStore, Operation, make_record
from tkn_objstorage_imgcatalog.config import load_config
from tkn_objstorage_imgcatalog.errors import AppError
from tkn_objstorage_imgcatalog.io import fingerprint
from tkn_objstorage_imgcatalog.notes import find_note, refresh_notes, split_note, urls
from tkn_objstorage_imgcatalog.storage import open_store
from tkn_objstorage_imgcatalog.sync import pull, push, verify

R2_ENDPOINT = "https://" + "a" * 32 + ".r2.cloudflarestorage.com"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)


def write_config(tmp_path, sources, version="3.0.0"):
    path = tmp_path / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        YAML().dump({"schema_version": version, "sources": sources}, stream)
    return path


def settings(provider):
    if provider == "azure":
        return {
            "provider": provider,
            "azure": {
                "account_url": "https://example.blob.core.windows.net",
                "container": "images",
            },
        }
    return {
        "provider": provider,
        "s3": {
            "bucket": "example-images",
            "region": "auto" if provider == "r2" else "ap-northeast-1",
            "endpoint_url": R2_ENDPOINT if provider == "r2" else None,
            "prefix": "photos/",
            "profile": "images",
        },
    }


def test_mixed_providers_have_isolated_targets_and_roots(tmp_path):
    config = load_config(write_config(tmp_path, {p: settings(p) for p in ("azure", "s3", "r2")}))
    keys = []
    for provider in ("azure", "s3", "r2"):
        selected = config.select_source(provider)
        assert selected.provider == provider
        assert selected.data_root == tmp_path / "home/.tkn/objstorage-imgcatalog/data" / provider
        keys.append(NoteStore(selected).target_key())
    assert len(set(keys)) == 3
    assert not (tmp_path / "home").exists()


@pytest.mark.parametrize("provider", ["s3", "r2"])
def test_same_bucket_cannot_be_registered_twice(tmp_path, provider):
    first = settings(provider)
    second = deepcopy(first)
    second["s3"]["prefix"] = "other/"
    with pytest.raises(AppError, match="one container or bucket"):
        load_config(write_config(tmp_path, {"one": first, "two": second}))


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "gcs"},
        {"s3": {"bucket": "BAD"}},
        {"s3": {"bucket": "ab"}},
        {"s3": {"bucket": "127.0.0.1"}},
        {"s3": {"bucket": "a..b"}},
        {"s3": {"prefix": "../escape"}},
        {"s3": {"timeout_seconds": 0}},
        {"s3": {"timeout_seconds": True}},
        {"s3": {"profile": ""}},
        {"s3": {"region": "bad/region"}},
        {"s3": {"secret_access_key": "not-accepted"}},
        {"s3": {"endpoint_url": "http://example.com"}},
        {"s3": {"endpoint_url": "https://user:password@example.com"}},
        {"s3": {"endpoint_url": "https://example.com?secret=value"}},
        {"s3": {"endpoint_url": "https://example.com/bucket"}},
        {"provider": "r2", "s3": {"endpoint_url": "https://public.example.com"}},
        {"provider": "r2", "s3": {"region": "ap-northeast-1"}},
        {"provider": "s3", "s3": {"region": "auto"}},
    ],
)
def test_invalid_provider_settings_are_readonly(tmp_path, change):
    path = write_config(tmp_path, {"images": change})
    before = path.read_bytes()
    with pytest.raises(AppError):
        load_config(path)
    assert path.read_bytes() == before
    assert not (tmp_path / "home").exists()


def test_legacy_config_discovery_keeps_data_and_prevents_shadowing(tmp_path, capsys):
    old = tmp_path / "home/.tkn/azure_blob_note"
    path = write_config(old, {"photos": {"azure": {"container": "images"}}}, "2.0.0")
    before = path.read_bytes()
    config = load_config()
    assert config.data_root == old / "data/photos"
    assert config.state_root == old / "state/photos"
    assert config.provider == "azure"
    assert config.loaded_sources[-1]["migrated"]
    assert cli.main(["config", "init"]) == 2
    assert "existing Azure config" in json.loads(capsys.readouterr().out)["error"]
    assert path.read_bytes() == before
    assert not (tmp_path / "home/.tkn/objstorage-imgcatalog").exists()
    write_config(tmp_path / "home/.tkn/objstorage-imgcatalog", {"new": settings("s3")})
    assert load_config().source_id == "new"


def test_old_azure_baseline_fingerprint_and_asset_ids_are_stable(cfg, asset):
    expected = fingerprint(
        {
            "library": str(cfg.data_root),
            **{key: cfg.azure[key] for key in ("account_url", "container", "prefix")},
        }
    )
    assert NoteStore(cfg).target_key() == expected
    NoteStore(cfg).set_baseline(asset, {"etag": "old"})
    changed = deepcopy(cfg)
    changed.source_values["provider"] = "s3"
    changed.s3.update(bucket="images", region="ap-northeast-1")
    assert NoteStore(changed).baseline(asset) is None
    record = make_record("a.png", "stable", source=None, digest="a" * 64, size=1)
    assert record["asset_id"] == str(uuid5(NAMESPACE_URL, "azure-blob-note:stable"))


def test_invalid_lower_provider_layer_cannot_be_hidden(tmp_path):
    old = tmp_path / "home/.tkn/objstorage-imgcatalog"
    invalid = settings("r2")
    invalid["s3"]["region"] = "ap-northeast-1"
    write_config(old, {"bad": invalid})
    explicit = write_config(tmp_path, {"good": settings("s3")})
    with pytest.raises(AppError, match="r2 region"):
        load_config(explicit)


def test_legacy_explicit_null_paths_remain_invalid(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        'schema_version: "1.0.0"\ndata_root: null\nstate_root: null\n', encoding="utf-8"
    )
    with pytest.raises(AppError, match="wrong type"):
        load_config(path)


@pytest.mark.parametrize("jurisdiction", ["", "eu.", "fedramp."])
def test_r2_jurisdiction_endpoints(tmp_path, jurisdiction):
    value = settings("r2")
    value["s3"]["endpoint_url"] = (
        "https://" + "a" * 32 + "." + jurisdiction + "r2.cloudflarestorage.com/"
    )
    config = load_config(write_config(tmp_path, {"images": value}))
    assert config.s3["endpoint_url"].endswith("r2.cloudflarestorage.com")


def test_note_migration_preserves_human_content_and_ids(cfg, asset):
    path = find_note(cfg, asset)
    text = path.read_text(encoding="utf-8").replace(
        'schemaVersion: "3.0.0"', "schemaVersion: 1.0.0"
    )
    text = text.replace("objectKey:", "blobName:").replace("objectUrl:", "blobUrl:")
    text = text.replace("object-storage-catalog:", "azure-blob-note:")
    text = text.replace("description:", "description: My description # keep")
    text += "\n## VLM description\nHuman-reviewed text.\n"
    path.write_text(text, encoding="utf-8")
    from tkn_objstorage_imgcatalog.io import atomic_json
    from tkn_objstorage_imgcatalog.migration import migrate

    atomic_json(cfg.data_root / "catalog" / (asset["asset_id"] + ".json"), asset)
    with Operation(cfg, "migrate", False) as operation:
        migrate(cfg, operation)
    result = path.read_text(encoding="utf-8")
    data, _ = split_note(result)
    assert data["assetId"] == asset["asset_id"]
    assert data["noteId"] == asset["note_id"]
    assert data["schemaVersion"] == "3.0.0"
    assert "blobName" not in data and "blobUrl" not in data
    assert data["objectKey"] == asset["relative_path"]
    assert "# keep" in result and "Human-reviewed text." in result
    assert result.count("<!-- object-storage-catalog:begin -->") == 1
    assert "azure-blob-note:" not in result
    assert refresh_notes(cfg, [asset["asset_id"]])[0]["status"] == "unchanged"


@pytest.mark.parametrize(
    "malformed",
    [
        "<!-- azure-blob-note:begin -->",
        "<!-- azure-blob-note:end --><!-- azure-blob-note:begin -->",
        "<!-- azure-blob-note:begin --><!-- azure-blob-note:end --><!-- object-storage-catalog:begin -->",
    ],
)
def test_malformed_legacy_markers_do_not_modify_note(cfg, asset, malformed):
    path = find_note(cfg, asset)
    text = path.read_text(encoding="utf-8")
    head = text[: text.index("<!-- object-storage-catalog:begin -->")]
    path.write_text(head + malformed, encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(AppError, match="markers"):
        refresh_notes(cfg, [])
    assert path.read_bytes() == before


@pytest.mark.parametrize("provider", ["azure", "s3", "r2"])
def test_provider_sync_and_readonly_previews(tmp_path, provider, asset, cfg, blobs):
    selected = deepcopy(cfg)
    selected.source_values.update(provider=provider)
    if provider != "azure":
        selected.s3.update(settings(provider)["s3"])
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with Operation(selected, "push", True) as operation:
        assert push(selected, [], operation, blobs)[0]["status"] == "created"
    assert blobs.writes == 0
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with Operation(selected, "push", False) as operation:
        push(selected, [], operation, blobs, yes=True)
    assert verify(selected, blobs=blobs)[0]["status"] == "valid"
    blobs.put(asset["relative_path"], b"updated bytes")
    with Operation(selected, "pull", False) as operation:
        pull(selected, [], operation, blobs)
    assert NoteStore(selected).release(asset).read_bytes() == b"updated bytes"


def test_r2_api_url_is_not_used_as_public_delivery_url(tmp_path):
    config = load_config(write_config(tmp_path, {"images": settings("r2")}))
    api, delivery = urls(config, "日本 image.webp")
    assert api.startswith(R2_ENDPOINT + "/example-images/photos/")
    assert "%20" in api and "%E6" in api
    assert delivery is None
    config.delivery["url_base"] = "https://images.example.com"
    assert urls(config, "a.webp")[1] == "https://images.example.com/a.webp"


def test_s3_url_includes_prefix_and_region(tmp_path):
    config = load_config(write_config(tmp_path, {"images": settings("s3")}))
    api, delivery = urls(config, "a #.webp")
    assert api == "https://s3.ap-northeast-1.amazonaws.com/example-images/photos/a%20%23.webp"
    assert delivery == api


@pytest.mark.parametrize("provider", ["azure", "s3", "r2"])
def test_factory_selects_the_matching_adapter(tmp_path, monkeypatch, provider):
    import tkn_objstorage_imgcatalog.azure as azure
    import tkn_objstorage_imgcatalog.s3 as s3

    config = load_config(write_config(tmp_path, {"images": settings(provider)}))
    factory = Mock()
    monkeypatch.setattr(
        azure if provider == "azure" else s3,
        "AzureBlobs" if provider == "azure" else "S3Objects",
        factory,
    )
    assert open_store(config) == factory.return_value
    factory.assert_called_once_with(config)


@pytest.mark.parametrize(
    "error",
    [
        NoCredentialsError(),
        ClientError(
            {
                "Error": {"Code": "AccessDenied", "Message": "credential-secret"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "PutObject",
        ),
        ClientError(
            {
                "Error": {"Code": "PreconditionFailed", "Message": "credential-secret"},
                "ResponseMetadata": {"HTTPStatusCode": 412},
            },
            "PutObject",
        ),
    ],
)
def test_sdk_errors_are_bounded_and_credential_free(tmp_path, monkeypatch, capsys, error):
    path = write_config(tmp_path, {"images": settings("s3")})
    monkeypatch.setattr(cli, "open_store", Mock(side_effect=error))
    assert cli.main(["--config", str(path), "push", "--source", "images", "--dry-run"]) == 3
    output = capsys.readouterr()
    assert "credential-secret" not in output.out + output.err
    assert json.loads(output.out)["status"] == "failed"
    assert not (tmp_path / "home").exists()

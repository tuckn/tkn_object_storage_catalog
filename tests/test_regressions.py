from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.core import MatchConditions
from PIL import Image

from tkn_objstorage_imgcatalog.assets import NoteStore, Operation
from tkn_objstorage_imgcatalog.azure import AzureBlobs
from tkn_objstorage_imgcatalog.cli import main
from tkn_objstorage_imgcatalog.config import load_config
from tkn_objstorage_imgcatalog.errors import AppError, ConflictError
from tkn_objstorage_imgcatalog.images import import_images
from tkn_objstorage_imgcatalog.io import atomic_bytes
from tkn_objstorage_imgcatalog.notes import find_note
from tkn_objstorage_imgcatalog.sync import push, status


@pytest.mark.parametrize("listed_etag", ["0x8ABC", '"0x8ABC"'])
def test_azure_list_and_head_etags_report_unchanged(cfg, asset, listed_etag):
    def properties(etag):
        return AzureBlobs.properties(
            asset["relative_path"],
            SimpleNamespace(
                etag=etag,
                size=asset["release"]["bytes"],
                metadata={},
                version_id=None,
                content_settings=SimpleNamespace(cache_control=None),
            ),
        )

    head = properties('"0x8ABC"')
    listed = properties(listed_etag)
    assert listed["etag"] == head["etag"] == '"0x8ABC"'
    assert properties("0x8ABD")["etag"] != head["etag"]
    NoteStore(cfg).set_baseline(asset, head)
    store = Mock()
    store.list.return_value = [listed]
    assert status(cfg, blobs=store)[0]["remote"] == "unchanged"


def test_readonly_note_collision_preflight(cfg, source):
    note = cfg.notes_root / "example.webp.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Own note", encoding="utf-8")
    for preview in (True, False):
        with pytest.raises(ConflictError):
            with Operation(cfg, "import", preview) as operation:
                import_images(cfg, [source], operation)
        assert not (cfg.data_root / "originals").exists()
        assert not (cfg.data_root / "releases").exists()


def test_atomic_create_does_not_replace_existing(tmp_path):
    path = tmp_path / "destination"
    path.write_bytes(b"user data")
    with pytest.raises(ConflictError):
        atomic_bytes(path, b"new", create_only=True)
    assert path.read_bytes() == b"user data"


def test_png_color_key_transparency_retained(cfg, source):
    Image.new("RGB", (8, 8), "red").save(source, transparency=(255, 0, 0))
    with Operation(cfg, "import", False) as operation:
        import_images(cfg, [source], operation)
    record = NoteStore(cfg).assets()[0]
    with Image.open(NoteStore(cfg).release(record)) as result:
        assert result.convert("RGBA").getpixel((0, 0))[3] == 0


def test_case_collision_push(cfg, asset, blobs):
    blobs.put(asset["relative_path"].upper(), b"other image")
    with pytest.raises(ConflictError):
        with Operation(cfg, "push", False) as operation:
            push(cfg, [], operation, blobs)
    assert blobs.writes == 0


def test_no_runtime_dotenv(tmp_path):
    (tmp_path / ".env").write_text("AZURE_STORAGE_ACCOUNT=shouldnotload")
    config = load_config(home=tmp_path / "absent", cwd=tmp_path)
    assert config.azure["account_url"] is None


def test_quiet_verbose_across_argument_levels(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as error:
        main(["--quiet", "status", "--verbose"])
    assert error.value.code == 2


def test_bad_extension_when_no_conversion(cfg, source):
    cfg.conversion["enabled"] = False
    with pytest.raises(AppError):
        with Operation(cfg, "import", True) as operation:
            import_images(cfg, [source], operation, name="misleading.webp")


def test_sdk_update_uses_etag_and_preserves_metadata(cfg, asset):
    adapter = AzureBlobs.__new__(AzureBlobs)
    adapter.config, adapter.prefix, adapter.timeout = cfg, "", 60
    adapter.container = Mock()
    previous = {"etag": "revision-1", "metadata": {"custom": "keep"}, "cache_control": "private"}
    current = {"etag": "revision-2"}
    adapter.get = Mock(return_value=current)
    adapter.digest = Mock(return_value=asset["release"]["sha256"])
    adapter.upload(asset["relative_path"], NoteStore(cfg).release(asset), asset, previous)
    call = adapter.container.get_blob_client.return_value.upload_blob.call_args
    assert call.kwargs["etag"] == "revision-1"
    assert call.kwargs["match_condition"] == MatchConditions.IfNotModified
    assert call.kwargs["metadata"]["custom"] == "keep"
    assert call.kwargs["content_settings"].cache_control == "private"


def test_sdk_create_does_not_overwrite(cfg, asset):
    adapter = AzureBlobs.__new__(AzureBlobs)
    adapter.config, adapter.prefix, adapter.timeout = cfg, "scope/", 60
    adapter.container = Mock()
    adapter.get = Mock(return_value={"etag": "new"})
    adapter.digest = Mock(return_value=asset["release"]["sha256"])
    adapter.upload(asset["relative_path"], NoteStore(cfg).release(asset), asset, None)
    call = adapter.container.get_blob_client.return_value.upload_blob.call_args
    assert call.kwargs["overwrite"] is False
    assert adapter.name(asset["relative_path"]) == "scope/" + asset["relative_path"]


def test_sdk_download_is_conditional(cfg):
    adapter = AzureBlobs.__new__(AzureBlobs)
    adapter.config, adapter.prefix, adapter.timeout = cfg, "", 60
    adapter.container = Mock()
    adapter.container.get_blob_client.return_value.download_blob.return_value.readall.return_value = b"exact"
    assert adapter.download("image.webp", "revision") == b"exact"
    assert (
        adapter.container.get_blob_client.return_value.download_blob.call_args.kwargs["etag"]
        == "revision"
    )


def test_different_libraries_do_not_share_baselines(cfg, asset):
    first = NoteStore(cfg)
    first.set_baseline(asset, {"etag": "remote"})
    second_cfg = deepcopy(cfg)
    second_cfg.source_values["data_root"] = str(cfg.data_root.parent / "other-library")
    assert NoteStore(second_cfg).baseline(asset) is None


def test_future_note_schema_stops_push_preview(cfg, asset, blobs):
    path = find_note(cfg, asset)
    path.write_text(
        path.read_text().replace('schemaVersion: "3.0.0"', "schemaVersion: 99.0.0"),
        encoding="utf-8",
    )
    with pytest.raises(AppError):
        with Operation(cfg, "push", True) as operation:
            push(cfg, [], operation, blobs)
    assert blobs.writes == 0

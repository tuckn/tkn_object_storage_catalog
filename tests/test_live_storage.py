from copy import deepcopy
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from live_storage import (
    LiveRun,
    checked_key,
    cleanup_r2,
    isolated_settings,
    run_prefix,
    safe_error,
    target_digest,
    validate_target,
)

from tkn_object_storage_catalog import config as configuration

RUN_ID = "20260101T000000Z-" + "a" * 32
RELATIVE = "adapter/条件 画像.png"
DIGEST = "b" * 64


def target(provider="r2"):
    return {
        "provider": provider,
        "endpoint_url": (
            "https://" + "a" * 32 + ".r2.cloudflarestorage.com"
            if provider == "r2"
            else "https://example.blob.core.windows.net"
        ),
        "bucket": "example-tests",
        "cleanup": "manifest" if provider == "r2" else "retain",
    }


@pytest.mark.parametrize("provider", ["azure", "r2"])
def test_accepts_reviewed_target(provider):
    value = target(provider)
    validate_target(value, target_digest(value))


@pytest.mark.parametrize(
    "change",
    [
        {"bucket": "production-images"},
        {"endpoint_url": "https://example.com"},
        {"endpoint_url": "https://user:secret@example.com"},
        {"cleanup": "retain"},
        {"provider": "s3"},
        {"secret_access_key": "do-not-store"},
    ],
)
def test_rejects_unsafe_contract_even_if_rehashed(change):
    value = target() | change
    with pytest.raises(AssertionError):
        validate_target(value, target_digest(value))


def test_changed_target_requires_new_independent_review():
    value = target()
    digest = target_digest(value)
    value["bucket"] = "different-tests"
    with pytest.raises(AssertionError):
        validate_target(value, digest)


@pytest.mark.parametrize("value", ["", "runs", "..", "*", RUN_ID + "/other", "../" + RUN_ID])
def test_invalid_run_scope_is_rejected(value):
    with pytest.raises(AssertionError):
        run_prefix(value)


@pytest.mark.parametrize("relative", ["../other.png", "other.png", "", "/adapter/条件 画像.png"])
def test_only_generated_names_can_be_mutation_targets(relative):
    with pytest.raises(AssertionError):
        checked_key(RUN_ID, relative)


def cleanup_fixture(run_id=RUN_ID):
    value = target()
    manifest = {
        RELATIVE: {
            "key": checked_key(run_id, RELATIVE),
            "asset_id": "generated-id",
            "sha256": [DIGEST],
        }
    }
    store = Mock()
    store.config.target_identity = {
        "provider": "r2",
        "endpoint_url": value["endpoint_url"],
        "bucket": value["bucket"],
        "prefix": run_prefix(run_id).rstrip("/"),
    }
    store.get.return_value = {"metadata": {"asset_id": "generated-id"}, "etag": "revision"}
    store.digest.return_value = DIGEST
    return value, manifest, store


def test_cleanup_deletes_manifest_only():
    value, manifest, store = cleanup_fixture()
    assert cleanup_r2(value, target_digest(value), RUN_ID, manifest, store) == 1
    store.client.delete_object.assert_called_once_with(
        Bucket=value["bucket"], Key=checked_key(RUN_ID, RELATIVE)
    )
    store.client.get_paginator.assert_not_called()


@pytest.mark.parametrize("failure", ["key", "manifest", "identity", "owner", "bytes"])
def test_cleanup_rejects_target_or_ownership_mismatch_before_delete(failure):
    value, manifest, store = cleanup_fixture()
    if failure == "key":
        manifest[RELATIVE]["key"] = "runs/another-run/image.png"
    elif failure == "manifest":
        manifest["unrelated.png"] = deepcopy(manifest[RELATIVE])
    elif failure == "identity":
        store.config.target_identity["bucket"] = "production-images"
    elif failure == "owner":
        store.get.return_value["metadata"]["asset_id"] = "someone-else"
    else:
        store.digest.return_value = "c" * 64
    with pytest.raises(AssertionError):
        cleanup_r2(value, target_digest(value), RUN_ID, manifest, store)
    store.client.delete_object.assert_not_called()


def test_azure_cleanup_is_rejected():
    value = target("azure")
    with pytest.raises(AssertionError):
        cleanup_r2(value, target_digest(value), RUN_ID, {}, Mock())


def test_sanitized_error_excludes_service_message_and_request():
    error = ClientError(
        {
            "Error": {"Code": "secret-code", "Message": "secret-value"},
            "ResponseMetadata": {
                "HTTPStatusCode": 403,
                "HTTPHeaders": {"Authorization": "secret-value"},
            },
        },
        "GetObject",
    )
    assert safe_error(error) == {"type": "ClientError", "http_status": 403}


def test_azure_retains_objects_even_after_scenario_failure(tmp_path):
    value = target("azure")
    run = LiveRun(value, target_digest(value), tmp_path)
    run.store = Mock()
    run.scenario = Mock(side_effect=RuntimeError("must not appear in report"))
    run.remote_keys = Mock(return_value=[run.prefix + RELATIVE])
    assert run.execute() == 1
    assert run.report["cleanup"] == {
        "policy": "retain",
        "status": "retained_for_lifecycle",
        "remaining": 1,
    }
    run.store.client.delete_object.assert_not_called()
    run.store.container.delete_blob.assert_not_called()
    assert "must not appear" not in (run.root / "report.json").read_text()


def test_r2_cleans_manifest_on_failure(tmp_path, monkeypatch):
    value = target()
    run = LiveRun(value, target_digest(value), tmp_path)
    _, run.manifest, run.store = cleanup_fixture(run.run_id)
    run.scenario = Mock(side_effect=RuntimeError("safe failure"))
    run.remote_keys = Mock(return_value=[])
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fake")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    assert run.execute() == 1
    assert run.report["cleanup"]["deleted"] == 1
    assert run.report["cleanup"]["remaining"] == 0


def test_normal_config_is_never_read_and_roots_are_isolated(tmp_path):
    value = target("azure")
    run = LiveRun(value, target_digest(value), tmp_path)
    old_user_root = configuration.user_root
    with isolated_settings(run.work_root):
        _, first = run.settings("first")
        _, second = run.settings("second")
        assert configuration.user_root() == run.work_root / "empty-user"
        roots = [
            getattr(cfg, key)
            for cfg in (first, second)
            for key in ("data_root", "state_root", "notes_root")
        ]
        assert len(set(roots)) == 6
        assert all(root.is_relative_to(run.work_root) for root in roots)
        assert all(not root.exists() for root in roots)
    assert configuration.user_root is old_user_root


def test_generated_image_import_uses_short_isolated_workspace(tmp_path):
    from PIL import Image

    from tkn_object_storage_catalog.catalog import Catalog

    value = target("azure")
    run = LiveRun(value, target_digest(value), tmp_path)
    with isolated_settings(run.work_root):
        path, config = run.settings("sender")
        source = run.work_root / "generated.png"
        Image.new("RGB", (24, 16), "red").save(source)
        run.command(path, "import", source, "--name", "nested/生成 画像.png")
        assert Catalog(config).assets()[0]["relative_path"] == "nested/生成 画像.webp"

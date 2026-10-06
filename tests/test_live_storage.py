import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from live_storage import (
    LiveRun,
    checked_key,
    cleanup_r2,
    isolated_settings,
    load_test_target,
    run_prefix,
    safe_error,
    target_digest,
    validate_target,
)
from live_storage import (
    main as live_main,
)

from tkn_objstorage_imgcatalog import config as configuration
from tkn_objstorage_imgcatalog.errors import AppError

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

    from tkn_objstorage_imgcatalog.catalog import Catalog

    value = target("azure")
    run = LiveRun(value, target_digest(value), tmp_path)
    with isolated_settings(run.work_root):
        path, config = run.settings("sender")
        source = run.work_root / "generated.png"
        Image.new("RGB", (24, 16), "red").save(source)
        run.command(path, "import", source, "--name", "nested/生成 画像.png")
        assert Catalog(config).assets()[0]["relative_path"] == "nested/生成 画像.webp"


def write_test_config(path, **targets):
    value = {
        "schema_version": "3.1.0",
        "sources": {"ordinary": {"azure": {"container": "images"}}},
        "integration_tests": {
            name: item | {"expected_target_sha256": target_digest(item)}
            for name, item in targets.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return value


@pytest.mark.parametrize("provider", ["azure", "r2"])
def test_loads_named_target_from_shared_config_without_adding_sources(tmp_path, provider):
    path = tmp_path / "config.yaml"
    value = write_test_config(path, azure=target("azure"), r2=target())
    before = path.read_bytes()
    selected, expected = load_test_target(path, provider)
    assert selected == target(provider)
    assert expected == target_digest(selected)
    cfg = configuration.load_config(path, home=tmp_path / "empty", cwd=tmp_path)
    assert list(cfg.sources) == ["ordinary"]
    assert cfg.source_id == "ordinary"
    assert cfg.values["integration_tests"] == value["integration_tests"]
    assert path.read_bytes() == before
    assert not cfg.data_root.exists()


def test_test_targets_replace_as_a_whole_and_track_origin(tmp_path):
    user = tmp_path / "user" / "config.yaml"
    explicit = tmp_path / "explicit.yaml"
    write_test_config(user, azure=target("azure"), r2=target())
    write_test_config(explicit, r2=target())
    cfg = configuration.load_config(explicit, home=user.parent, cwd=tmp_path)
    assert list(cfg.values["integration_tests"]) == ["r2"]
    assert cfg.origins["integration_tests.r2.endpoint_url"] == str(explicit)
    assert not any(key.startswith("integration_tests.azure") for key in cfg.origins)


@pytest.mark.parametrize(
    "change",
    [
        {"cleanup": "manifest"},
        {"provider": "s3"},
        {"bucket": "production"},
        {"endpoint_url": "https://example.com?secret=hidden"},
        {"expected_target_sha256": "bad"},
        {"secret_access_key": "hidden-secret"},
        {"endpoint_url": None},
    ],
)
def test_invalid_shared_test_settings_are_rejected_without_writes(tmp_path, change):
    path = tmp_path / "config.yaml"
    value = write_test_config(path, azure=target("azure"))
    value["integration_tests"]["azure"].update(change)
    path.write_text(json.dumps(value), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(AppError) as exc:
        configuration.load_config(path, home=tmp_path / "empty", cwd=tmp_path)
    assert "hidden-secret" not in str(exc.value)
    assert path.read_bytes() == before


def test_loader_checks_digest_and_never_falls_back_to_other_target(tmp_path):
    path = tmp_path / "config.yaml"
    value = write_test_config(path, r2=target())
    with pytest.raises(AppError):
        load_test_target(path, "missing")
    value["integration_tests"]["r2"]["bucket"] = "other-tests"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(AssertionError):
        load_test_target(path, "r2")


def test_default_config_check_ignores_cwd_and_creates_no_run(tmp_path, monkeypatch, capsys):
    user_root = tmp_path / "user"
    path = user_root / "config.yaml"
    write_test_config(path, r2=target())
    cwd = tmp_path / "project"
    write_test_config(cwd / ".tkn/config.yaml", azure=target("azure"))
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(configuration, "user_root", lambda: user_root)
    forbidden = Mock(side_effect=AssertionError("Unexpected live execution"))
    monkeypatch.setattr("live_storage.LiveRun", forbidden)
    monkeypatch.setattr("live_storage.logging.disable", lambda level: None)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert live_main(["--check", "--test-target", "r2"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "valid"
    assert live_main(["--check", "--test-target", "azure"]) == 1
    capsys.readouterr()
    forbidden.assert_not_called()
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_test_settings_require_new_schema_and_can_be_cleared(tmp_path):
    user = tmp_path / "user" / "config.yaml"
    explicit = tmp_path / "explicit.yaml"
    value = write_test_config(user, azure=target("azure"))
    value["schema_version"] = "3.0.0"
    user.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(AppError, match="requires config schema 3.1.0"):
        configuration.load_config(home=user.parent, cwd=tmp_path)
    write_test_config(user, azure=target("azure"))
    explicit.write_text(
        json.dumps({"schema_version": "3.1.0", "integration_tests": {}}), encoding="utf-8"
    )
    cfg = configuration.load_config(explicit, home=user.parent, cwd=tmp_path)
    assert cfg.values["integration_tests"] == {}
    assert list(cfg.sources) == ["ordinary"]
    assert cfg.origins["integration_tests"] == str(explicit)
    assert not any(key.startswith("integration_tests.") for key in cfg.origins)


def test_explicit_run_uses_only_selected_target(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.yaml"
    write_test_config(path, azure=target("azure"), r2=target())
    run = Mock()
    run.execute.return_value = 0
    run.run_id = RUN_ID
    run.report = {"status": "passed", "checks": [], "cleanup": {"policy": "retain"}}
    factory = Mock(return_value=run)
    monkeypatch.setattr("live_storage.LiveRun", factory)
    monkeypatch.setattr("live_storage.logging.disable", lambda level: None)
    assert live_main(["--run", "--config", str(path), "--test-target", "azure"]) == 0
    assert factory.call_args.args[:2] == (target("azure"), target_digest(target("azure")))
    run.execute.assert_called_once_with()
    assert json.loads(capsys.readouterr().out)["provider"] == "azure"


S3_TARGET = {
    "provider": "s3",
    "endpoint_url": None,
    "region": "ap-northeast-1",
    "bucket": "example-catalog-tests",
    "cleanup": "manifest",
    "profile": "example-catalog-test",
    "account_id": "123456789012",
    "role_arn": "arn:aws:iam::123456789012:role/example-catalog-test",
}


def test_s3_config_preserves_existing_targets_and_sources(tmp_path):
    path = tmp_path / "config.yaml"
    value = write_test_config(path, azure=target("azure"), r2=target(), s3=S3_TARGET)
    selected, digest = load_test_target(path, "s3")
    assert selected == S3_TARGET
    cfg = configuration.load_config(path, home=tmp_path / "empty", cwd=tmp_path)
    assert list(cfg.sources) == ["ordinary"]
    assert cfg.values["integration_tests"] == value["integration_tests"]
    run = LiveRun(selected, digest, tmp_path / "reports")
    _, settings = run.settings("sender")
    assert settings.provider == "s3"
    assert settings.s3["profile"] == S3_TARGET["profile"]
    assert settings.s3["region"] == S3_TARGET["region"]
    assert settings.s3["endpoint_url"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"endpoint_url": "https://s3.ap-northeast-1.amazonaws.com"},
        {"region": "auto"},
        {"profile": "default"},
        {"account_id": 123456789012},
        {"role_arn": "arn:aws:iam::999999999999:role/example-catalog-test"},
        {"cleanup": "retain"},
        {"session_token": "secret"},
    ],
)
def test_s3_rejects_invalid_identity_or_endpoint(change):
    value = S3_TARGET | change
    with pytest.raises(AssertionError):
        validate_target(value, target_digest(value))


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile", "different-test"),
        ("region", "us-east-1"),
        ("account_id", "999999999999"),
        ("role_arn", "arn:aws:iam::123456789012:role/other-test"),
        ("bucket", "other-tests"),
    ],
)
def test_s3_identity_fields_are_bound_to_reviewed_digest(field, value):
    with pytest.raises(AssertionError):
        validate_target(S3_TARGET | {field: value}, target_digest(S3_TARGET))


@pytest.mark.parametrize(
    "identity,accepted",
    [
        (
            {
                "Account": "123456789012",
                "Arn": "arn:aws:sts::123456789012:assumed-role/example-catalog-test/session",
            },
            True,
        ),
        (
            {
                "Account": "999999999999",
                "Arn": "arn:aws:sts::123456789012:assumed-role/example-catalog-test/session",
            },
            False,
        ),
        ({"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/example"}, False),
        (
            {
                "Account": "123456789012",
                "Arn": "arn:aws:sts::123456789012:assumed-role/other-test/session",
            },
            False,
        ),
    ],
)
def test_s3_checks_assumed_role_using_explicit_profile(monkeypatch, identity, accepted):
    from unittest.mock import MagicMock

    from live_storage import verify_s3_identity

    session = MagicMock()
    session.return_value.client.return_value.__enter__.return_value.get_caller_identity.return_value = identity
    monkeypatch.setattr("live_storage.boto3.Session", session)
    monkeypatch.setenv("AWS_PROFILE", "unrelated")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "injected-r2")
    if accepted:
        verify_s3_identity(S3_TARGET)
    else:
        with pytest.raises(AssertionError):
            verify_s3_identity(S3_TARGET)
    session.assert_called_once_with(profile_name=S3_TARGET["profile"])
    assert session.return_value.client.call_args.kwargs["config"].ignore_configured_endpoint_urls


def s3_cleanup_fixture(run_id=RUN_ID):
    _, manifest, store = cleanup_fixture(run_id)
    store.config.target_identity.update(
        provider="s3", endpoint_url="aws", bucket=S3_TARGET["bucket"]
    )
    store.config.s3 = {"profile": S3_TARGET["profile"], "region": S3_TARGET["region"]}
    return manifest, store


def test_s3_cleanup_uses_verified_etag():
    from live_storage import cleanup_s3

    manifest, store = s3_cleanup_fixture()
    assert cleanup_s3(S3_TARGET, target_digest(S3_TARGET), RUN_ID, manifest, store) == 1
    store.client.delete_object.assert_called_once_with(
        Bucket=S3_TARGET["bucket"], Key=checked_key(RUN_ID, RELATIVE), IfMatch="revision"
    )
    store.client.get_paginator.assert_not_called()


@pytest.mark.parametrize(
    "failure", ["key", "manifest", "identity", "owner", "bytes", "profile", "region"]
)
def test_s3_cleanup_refuses_unverified_objects(failure):
    from live_storage import cleanup_s3

    manifest, store = s3_cleanup_fixture()
    if failure == "key":
        manifest[RELATIVE]["key"] = "runs/other/image.png"
    elif failure == "manifest":
        manifest["unrelated.png"] = deepcopy(manifest[RELATIVE])
    elif failure == "identity":
        store.config.target_identity["bucket"] = "other-tests"
    elif failure == "owner":
        store.get.return_value["metadata"]["asset_id"] = "different"
    elif failure == "bytes":
        store.digest.return_value = "c" * 64
    else:
        store.config.s3[failure] = "different"
    with pytest.raises(AssertionError):
        cleanup_s3(S3_TARGET, target_digest(S3_TARGET), RUN_ID, manifest, store)
    store.client.delete_object.assert_not_called()


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_s3_finally_cleans_after_scenario_failure(tmp_path, monkeypatch, cleanup_failure):
    run = LiveRun(S3_TARGET, target_digest(S3_TARGET), tmp_path)
    run.manifest, run.store = s3_cleanup_fixture(run.run_id)
    run.scenario = Mock(side_effect=RuntimeError("private error"))
    run.remote_keys = Mock(return_value=[])
    monkeypatch.setattr("live_storage.verify_s3_identity", lambda target: None)
    if cleanup_failure:
        run.store.client.delete_object.side_effect = ClientError(
            {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
            "DeleteObject",
        )
    assert run.execute() == 1
    assert run.report["cleanup"]["status"] == ("failed" if cleanup_failure else "passed")
    if cleanup_failure:
        assert run.report["cleanup"]["error"]["http_status"] == 412
    else:
        assert run.report["cleanup"]["deleted"] == 1


def test_identity_failure_prevents_any_storage_connection(tmp_path, monkeypatch):
    run = LiveRun(S3_TARGET, target_digest(S3_TARGET), tmp_path)
    monkeypatch.setattr("live_storage.verify_s3_identity", Mock(side_effect=AssertionError()))
    run.scenario = Mock()
    assert run.execute() == 1
    run.scenario.assert_not_called()
    assert run.report["cleanup"]["status"] == "not_connected"


@pytest.mark.parametrize(
    "status,code,accepted",
    [
        (403, "AccessDenied", True),
        (401, "AccessDenied", False),
        (403, "InvalidAccessKeyId", False),
        (404, "NoSuchKey", False),
        (400, "InvalidArgument", False),
    ],
)
def test_s3_anonymous_requires_403_access_denied(monkeypatch, status, code, accepted):
    from io import BytesIO
    from urllib.error import HTTPError

    from live_storage import anonymous_denied

    error = HTTPError(
        "https://example.invalid",
        status,
        "",
        {},
        BytesIO(f"<Error><Code>{code}</Code><Message>Authorization</Message></Error>".encode()),
    )
    monkeypatch.setattr("live_storage.urlopen", Mock(side_effect=error))
    if accepted:
        anonymous_denied("https://example.invalid", "s3")
    else:
        with pytest.raises(AssertionError):
            anonymous_denied("https://example.invalid", "s3")

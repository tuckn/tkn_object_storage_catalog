from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ProfileNotFound,
)
from ruamel.yaml import YAML

from tkn_objstorage_imgcatalog import cli
from tkn_objstorage_imgcatalog.diagnostics import diagnostic_message, storage_diagnostic


def denied(code="AccessDenied", operation="ListObjectsV2", status=403):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "credential-secret"},
            "ResponseMetadata": {"HTTPStatusCode": status, "RequestId": "credential-secret"},
        },
        operation,
    )


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def config_path(tmp_path, provider):
    path = tmp_path / "config.yaml"
    with path.open("w", encoding="utf-8") as stream:
        YAML().dump(
            {
                "schema_version": "4.0.0",
                "sources": {
                    "images": {
                        "provider": provider,
                        "data_root": str(tmp_path / "data"),
                        "state_root": str(tmp_path / "state"),
                        "s3": {
                            "bucket": "example-images",
                            "profile": "images",
                            "region": "auto" if provider == "r2" else "ap-northeast-1",
                            "endpoint_url": "https://" + "a" * 32 + ".r2.cloudflarestorage.com"
                            if provider == "r2"
                            else None,
                        },
                    }
                },
            },
            stream,
        )
    return path


@pytest.mark.parametrize(
    "provider,service,permission",
    [
        ("r2", "Cloudflare R2", "R2 API token"),
        ("s3", "AWS S3", "AWS IAM"),
    ],
)
@pytest.mark.parametrize("phase", ["connect", "list"])
def test_failure_is_recorded_and_matches_console(
    isolated, monkeypatch, capsys, provider, service, permission, phase
):
    path = config_path(isolated, provider)
    store = Mock()
    if phase == "connect":
        monkeypatch.setattr(cli, "open_store", Mock(side_effect=denied()))
    else:
        store.list.side_effect = denied()
        monkeypatch.setattr(cli, "open_store", Mock(return_value=store))
    assert cli.main(["pull", "--source", "images", "--config", str(path), "--quiet"]) == 3
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["source_id"] == "images"
    assert payload["error"].startswith(service)
    assert permission in payload["error"]
    assert payload["diagnostics"]["error_code"] == "AccessDenied"
    assert payload["diagnostics"]["http_status"] == 403
    assert payload["diagnostics"]["operation"] == "ListObjectsV2"
    runs = list((isolated / "state/runs").glob("*.json"))
    assert len(runs) == 1
    run = json.loads(runs[0].read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    assert run["provider"] == provider
    assert run["diagnostics"] == payload["diagnostics"]
    assert run["error"] == payload["error"]
    log = (isolated / "state/logs" / (run["run_id"] + ".log")).read_text(encoding="utf-8")
    assert payload["error"] in log
    assert output.err.count("[ERROR]") == 1
    assert "credential-secret" not in output.out + output.err + log + runs[0].read_text()
    assert "S3/R2" not in payload["error"]
    if phase == "list":
        store.close.assert_called_once()


@pytest.mark.parametrize(
    "command,flags", [("pull", ["--dry-run"]), ("status", ["--remote"]), ("verify", ["--remote"])]
)
def test_readonly_failures_do_not_persist(isolated, monkeypatch, capsys, command, flags):
    path = config_path(isolated, "r2")
    monkeypatch.setattr(cli, "open_store", Mock(side_effect=NoCredentialsError()))
    assert cli.main([command, "--source", "images", "--config", str(path), *flags]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["diagnostics"]["error_type"] == "NoCredentialsError"
    assert "R2 Access Key" in payload["error"]
    assert not (isolated / "data").exists()
    assert not (isolated / "state").exists()
    assert not (isolated / "home").exists()


@pytest.mark.parametrize(
    "exc,expected",
    [
        (NoCredentialsError(), "R2 Access Key"),
        (ProfileNotFound(profile="credential-secret"), "s3.profile"),
        (EndpointConnectionError(endpoint_url="https://credential-secret.invalid"), "DNS"),
        (denied("SignatureDoesNotMatch"), "R2 Access Key"),
        (denied("NoSuchBucket", status=404), "s3.bucket"),
        (denied("PreconditionFailed", status=412), "status --remote"),
    ],
)
def test_diagnostics_are_actionable_without_exception_text(exc, expected):
    details = storage_diagnostic(exc, "r2")
    assert details is not None
    message = diagnostic_message(details)
    assert expected in message
    assert "credential-secret" not in message + json.dumps(details)
    assert "AWS IAM" not in message


def test_untrusted_identifiers_are_not_echoed():
    details = storage_diagnostic(
        denied("credential-secret", "credential-secret", "credential-secret"), "r2"
    )
    assert details["error_code"] == "Unknown"
    assert "http_status" not in details
    assert "operation" not in details
    assert "credential-secret" not in json.dumps(details)


def test_connection_failure_before_first_request_is_persisted(isolated, monkeypatch, capsys):
    path = config_path(isolated, "r2")
    monkeypatch.setattr(
        cli, "open_store", Mock(side_effect=ProfileNotFound(profile="credential-secret"))
    )
    assert cli.main(["pull", "--source", "images", "--config", str(path)]) == 3
    payload = json.loads(capsys.readouterr().out)
    run = json.loads(next((isolated / "state/runs").glob("*.json")).read_text())
    assert run["diagnostics"] == payload["diagnostics"]
    assert run["error_type"] == "ProfileNotFound"
    assert run["events"] == []

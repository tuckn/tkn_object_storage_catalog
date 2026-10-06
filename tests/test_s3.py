from __future__ import annotations

import base64
import hashlib
from io import BytesIO
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

import tkn_objstorage_imgcatalog.s3 as module
from tkn_objstorage_imgcatalog.catalog import Catalog, Operation
from tkn_objstorage_imgcatalog.errors import AppError, ConflictError
from tkn_objstorage_imgcatalog.s3 import S3Objects
from tkn_objstorage_imgcatalog.sync import push


@pytest.fixture(params=["s3", "r2"])
def adapter(cfg, request, monkeypatch):
    cfg.source_values["provider"] = request.param
    cfg.s3.update(bucket="images", prefix="photos", region="ap-northeast-1")
    if request.param == "r2":
        cfg.s3.update(
            endpoint_url="https://" + "a" * 32 + ".r2.cloudflarestorage.com", region="auto"
        )
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        endpoint_url="https://storage.example.com",
    )
    session = Mock()
    session.client.return_value = client
    monkeypatch.setattr(module.boto3, "Session", Mock(return_value=session))
    result = S3Objects(cfg)
    options = session.client.call_args.kwargs
    assert options["region_name"] == ("auto" if request.param == "r2" else "ap-northeast-1")
    assert options["config"].request_checksum_calculation == "when_required"
    assert options["config"].ignore_configured_endpoint_urls
    yield result
    result.close()


def head(etag='"revision"', payload=b"image", **extra):
    return {"ETag": etag, "ContentLength": len(payload), "Metadata": {}, **extra}


def get_response(payload=b"image", etag='"revision"'):
    return {
        "ETag": etag,
        "ContentLength": len(payload),
        "Body": StreamingBody(BytesIO(payload), len(payload)),
    }


def test_paginated_list_fetches_metadata_and_filters_non_images(adapter):
    with Stubber(adapter.client) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [{"Key": "photos/b.webp", "ETag": '"b"'}, {"Key": "photos/readme.txt"}],
                "IsTruncated": True,
                "NextContinuationToken": "page2",
            },
            {"Bucket": "images", "Prefix": "photos/"},
        )
        stub.add_response(
            "head_object",
            head('"b"', Metadata={"custom": "kept"}, CacheControl="public"),
            {
                "Bucket": "images",
                "Key": "photos/b.webp",
                "IfMatch": '"b"',
            },
        )
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [{"Key": "photos/a.png", "ETag": '"a"'}],
                "IsTruncated": False,
            },
            {"Bucket": "images", "Prefix": "photos/", "ContinuationToken": "page2"},
        )
        stub.add_response(
            "head_object",
            head('"a"'),
            {
                "Bucket": "images",
                "Key": "photos/a.png",
                "IfMatch": '"a"',
            },
        )
        result = adapter.list()
        assert [item["relative_path"] for item in result] == ["a.png", "b.webp"]
        assert result[1]["metadata"] == {"custom": "kept"}
        assert result[1]["cache_control"] == "public"
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("key", ["photos/../escape.webp", "outside/image.webp", "photos/CON.webp"])
def test_unsafe_keys_rejected_before_download(adapter, key):
    with Stubber(adapter.client) as stub:
        stub.add_response("list_objects_v2", {"Contents": [{"Key": key, "ETag": '"x"'}]})
        with pytest.raises(AppError):
            adapter.list()
        stub.assert_no_pending_responses()


def test_case_collisions_rejected(adapter):
    with Stubber(adapter.client) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [
                    {"Key": "photos/a.webp", "ETag": '"a"'},
                    {"Key": "photos/A.webp", "ETag": '"b"'},
                ]
            },
        )
        stub.add_response("head_object", head('"a"'))
        with pytest.raises(ConflictError, match="collide"):
            adapter.list()


def test_only_not_found_is_missing(adapter):
    with Stubber(adapter.client) as stub:
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
        assert adapter.get("a.webp") is None
        for code, status in (("AccessDenied", 403), ("NoSuchBucket", 404)):
            stub.add_client_error("head_object", service_error_code=code, http_status_code=status)
            with pytest.raises(ClientError):
                adapter.get("a.webp")


def test_digest_and_download_use_conditional_get_and_close_streams(adapter):
    expected = {"Bucket": "images", "Key": "photos/a.webp", "IfMatch": '"revision"'}
    with Stubber(adapter.client) as stub:
        first, second = get_response(), get_response()
        stub.add_response("get_object", first, expected)
        stub.add_response("get_object", second, expected)
        assert adapter.digest("a.webp", '"revision"') == hashlib.sha256(b"image").hexdigest()
        assert adapter.download("a.webp", '"revision"') == b"image"
        assert first["Body"]._raw_stream.closed and second["Body"]._raw_stream.closed


def test_unexpected_download_revision_is_rejected(adapter):
    with Stubber(adapter.client) as stub:
        response = get_response(etag='"other"')
        stub.add_response("get_object", response)
        with pytest.raises(ConflictError):
            adapter.download("a.webp", '"revision"')
        assert response["Body"]._raw_stream.closed


@pytest.mark.parametrize("existing", [False, True])
def test_conditional_upload_preserves_metadata_and_verifies_bytes(adapter, cfg, asset, existing):
    path = Catalog(cfg).release(asset)
    payload = path.read_bytes()
    previous = (
        {"etag": '"old"', "metadata": {"custom": "keep"}, "cache_control": "private"}
        if existing
        else None
    )
    metadata = {"asset_id": asset["asset_id"], "sha256": asset["release"]["sha256"]}
    expected = {
        "Bucket": "images",
        "Key": "photos/example.webp",
        "Body": ANY,
        "ContentType": "image/webp",
        "ContentLength": len(payload),
        "ContentMD5": base64.b64encode(hashlib.md5(payload).digest()).decode(),
        "Metadata": metadata,
    }
    if existing:
        metadata["custom"] = "keep"
        expected.update(IfMatch='"old"', CacheControl="private")
    else:
        expected["IfNoneMatch"] = "*"
    with Stubber(adapter.client) as stub:
        stub.add_response("put_object", {"ETag": '"new"'}, expected)
        stub.add_response(
            "head_object",
            head('"new"', payload, Metadata=metadata),
            {
                "Bucket": "images",
                "Key": "photos/example.webp",
                "IfMatch": '"new"',
            },
        )
        stub.add_response(
            "get_object",
            get_response(payload, '"new"'),
            {
                "Bucket": "images",
                "Key": "photos/example.webp",
                "IfMatch": '"new"',
            },
        )
        assert adapter.upload("example.webp", path, asset, previous)["etag"] == '"new"'
        stub.assert_no_pending_responses()


def test_upload_rejects_mismatched_content(adapter, cfg, asset):
    with Stubber(adapter.client) as stub:
        stub.add_response("put_object", {"ETag": '"new"'})
        stub.add_response("head_object", head('"new"'))
        stub.add_response("get_object", get_response(b"changed", '"new"'))
        with pytest.raises(ConflictError, match="bytes differ"):
            adapter.upload("example.webp", Catalog(cfg).release(asset), asset, None)


@pytest.mark.parametrize("status", [409, 412])
def test_write_conflicts_do_not_retry_unconditionally(adapter, cfg, asset, status):
    with Stubber(adapter.client) as stub:
        stub.add_client_error(
            "put_object", service_error_code="PreconditionFailed", http_status_code=status
        )
        with pytest.raises(ClientError):
            adapter.upload("example.webp", Catalog(cfg).release(asset), asset, None)
        stub.assert_no_pending_responses()


def test_upload_limit_and_unknown_public_access(adapter, cfg, asset, monkeypatch):
    monkeypatch.setattr(module, "MAX_UPLOAD_BYTES", 1)
    with Stubber(adapter.client):
        with pytest.raises(AppError, match="single PUT"):
            adapter.upload("example.webp", Catalog(cfg).release(asset), asset, None)
    assert adapter.public_access() is None
    cfg.delivery["url_base"] = "https://images.example.com"
    assert adapter.public_access() is True


def test_required_connection_settings_checked_before_credentials(cfg, monkeypatch):
    cfg.source_values["provider"] = "r2"
    monkeypatch.setattr(
        module.boto3, "Session", Mock(side_effect=AssertionError("Credential lookup"))
    )
    with pytest.raises(AppError, match="bucket"):
        S3Objects(cfg)
    cfg.s3["bucket"] = "images"
    with pytest.raises(AppError, match="endpoint_url"):
        S3Objects(cfg)


def test_upload_limit_is_checked_by_readonly_preview(adapter, cfg, asset, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "MAX_UPLOAD_BYTES", 1)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with Stubber(adapter.client) as stub:
        stub.add_response("list_objects_v2", {"Contents": []})
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
        with pytest.raises(AppError, match="single PUT"):
            with Operation(cfg, "push", True) as operation:
                push(cfg, [], operation, adapter)
        stub.assert_no_pending_responses()
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

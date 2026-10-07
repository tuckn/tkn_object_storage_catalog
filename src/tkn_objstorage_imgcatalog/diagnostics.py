"""Bounded storage diagnostics shared by console output and persistent records."""

from __future__ import annotations

from typing import Any

from botocore import exceptions
from botocore.exceptions import BotoCoreError, ClientError

SERVICE_NAMES = {"s3": "AWS S3", "r2": "Cloudflare R2", "azure": "Azure Blob Storage"}

# Response strings are untrusted. Only known, non-sensitive identifiers are emitted.
ERROR_CODES = frozenset(
    {
        "AccessDenied",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "ExpiredToken",
        "InvalidToken",
        "TokenRefreshRequired",
        "NoSuchBucket",
        "NoSuchKey",
        "NotFound",
        "PermanentRedirect",
        "AuthorizationHeaderMalformed",
        "RequestTimeTooSkewed",
        "RequestExpired",
        "PreconditionFailed",
        "ConditionalRequestConflict",
        "SlowDown",
        "InternalError",
        "ServiceUnavailable",
        "RequestTimeout",
        "InvalidRequest",
        "NotImplemented",
        "InvalidArgument",
        "400",
        "401",
        "403",
        "404",
        "409",
        "412",
    }
)
OPERATIONS = frozenset(
    {
        "ListObjectsV2",
        "HeadObject",
        "GetObject",
        "PutObject",
        "HeadBucket",
        "GetPublicAccessBlock",
        "GetBucketPolicyStatus",
        "GetBucketAcl",
        "GetBucketPolicy",
    }
)


def storage_diagnostic(exc: BaseException, provider: str) -> dict[str, Any] | None:
    if not isinstance(exc, (BotoCoreError, ClientError)):
        return None
    name = type(exc).__name__
    # Do not trust arbitrary subclass names either.
    name = name if getattr(exceptions, name, None) is type(exc) else "SDKError"
    result: dict[str, Any] = {"provider": provider, "error_type": name}
    code = None
    status = None
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        result["error_code"] = code if isinstance(code, str) and code in ERROR_CODES else "Unknown"
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if type(status) is int and 100 <= status <= 599:
            result["http_status"] = status
        else:
            status = None
        if exc.operation_name in OPERATIONS:
            result["operation"] = exc.operation_name
    if provider == "r2":
        credentials = (
            "Check the selected profile/environment for R2 Access Key ID and Secret Access Key."
        )
        permissions = (
            "Check the R2 API token's object read/write permissions and allowed bucket scope."
        )
        endpoint = "Check the Cloudflare R2 account S3 API endpoint; region must be auto."
    else:
        credentials = "Check AWS credentials in the selected profile/environment; renew expired sessions (SSO if used)."
        permissions = "Check AWS IAM permissions, bucket policy, and any explicit deny."
        endpoint = "Check the AWS S3 bucket region and configured endpoint."
    hint = f"{credentials} {endpoint}"
    if name in {
        "NoCredentialsError",
        "PartialCredentialsError",
        "CredentialRetrievalError",
        "TokenRetrievalError",
        "UnauthorizedSSOTokenError",
        "SSOTokenLoadError",
    }:
        hint = credentials
    elif name in {"ProfileNotFound", "ConfigNotFound", "ConfigParseError", "InvalidConfigError"}:
        hint = (
            "Check s3.profile and the shared credentials/config files used by this process. "
            + credentials
        )
    elif name in {
        "EndpointConnectionError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "ConnectionClosedError",
        "ProxyConnectionError",
        "SSLError",
    }:
        hint = f"Check DNS, proxy, TLS certificates, and network connectivity. {endpoint}"
    elif code in {
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "ExpiredToken",
        "InvalidToken",
        "TokenRefreshRequired",
    }:
        hint = credentials
    elif code in {"RequestTimeTooSkewed", "RequestExpired"}:
        hint = "Check the computer clock and time synchronization."
    elif status in {409, 412}:
        hint = "The remote object changed. Inspect status --remote and resolve the conflict before retrying."
    elif code == "NoSuchBucket":
        hint = f"Check s3.bucket and that the bucket exists in the intended account. {endpoint}"
    elif code in {"NoSuchKey", "NotFound", "404"} or status == 404:
        hint = "Check the bucket, prefix, and object path; the requested resource was not found."
    elif code in {"PermanentRedirect", "AuthorizationHeaderMalformed"}:
        hint = endpoint
    elif code == "AccessDenied" or status in {401, 403}:
        hint = permissions
    elif code in {"SlowDown", "InternalError", "ServiceUnavailable", "RequestTimeout"} or (
        status is not None and status >= 500
    ):
        hint = "The service is temporarily unavailable or throttling requests. Retry later."
    result["hint"] = hint
    return result


def diagnostic_message(details: dict[str, Any]) -> str:
    parts = [details["error_type"]]
    for key in ("operation", "error_code", "http_status"):
        if key in details:
            parts.append(f"{key}={details[key]}")
    service = SERVICE_NAMES.get(details["provider"], "Object storage")
    return f"{service} request failed ({', '.join(parts)}). {details['hint']}"


class StorageRequestError(Exception):
    def __init__(self, details: dict[str, Any], source_id: str):
        super().__init__(diagnostic_message(details))
        self.details = details
        self.source_id = source_id

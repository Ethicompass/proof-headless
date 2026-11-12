"""Reading a run's objects out of Yield's store over the S3 API, signed with AWS Signature V4.

Yield's API hands out the run's manifest and nothing else (Yield `GET /runs/{id}/result`); the
traces, the dataset, the probe set pointer and whatever metric outputs were not yet delivered stay
in the store under `runs/<run id>/` (Yield `yield_store.layout`). Two read-only calls reach them,
`ListObjectsV2` and `GetObject`, and signing those by hand is a few lines of the standard library,
which keeps an S3 client library out of the appliance for what is one prefix copy per run.
Requests are path-style, as MinIO serves them.
"""

from __future__ import annotations

import hashlib
import hmac
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class StoreUnavailable(RuntimeError):
    """The store refused a request or could not be reached. The message never carries a key."""


@dataclass(frozen=True)
class Credentials:
    """An access key pair. The repr shows neither half."""

    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    region: str = "us-east-1"


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def _encoded(value: str, safe: str = "-_.~") -> str:
    return quote(value, safe=safe)


def sign(
    method: str,
    url: str,
    query: dict[str, str],
    credentials: Credentials,
    now: datetime,
) -> dict[str, str]:
    """The headers that sign an empty-bodied request to `url` with `query` (SigV4, service s3)."""
    parts = urlsplit(url)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    canonical_query = "&".join(
        f"{_encoded(key)}={_encoded(value)}" for key, value in sorted(query.items())
    )
    headers = {"host": parts.netloc, "x-amz-content-sha256": EMPTY_SHA256, "x-amz-date": stamp}
    signed = ";".join(sorted(headers))
    canonical = "\n".join(
        [
            method,
            _encoded(parts.path or "/", safe="/-_.~"),
            canonical_query,
            "".join(f"{name}:{headers[name]}\n" for name in sorted(headers)),
            signed,
            EMPTY_SHA256,
        ]
    )
    scope = f"{day}/{credentials.region}/s3/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = _hmac(("AWS4" + credentials.secret_key).encode(), day)
    for part in (credentials.region, "s3", "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "x-amz-content-sha256": EMPTY_SHA256,
        "x-amz-date": stamp,
        "authorization": (
            f"AWS4-HMAC-SHA256 Credential={credentials.access_key}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}"
        ),
    }


class Store:
    """One bucket of the S3 endpoint at `endpoint`, read through `client`."""

    def __init__(
        self, client: httpx.Client, endpoint: str, bucket: str, credentials: Credentials
    ) -> None:
        self.client = client
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.credentials = credentials

    def _get(self, path: str, query: dict[str, str]) -> httpx.Response:
        url = f"{self.endpoint}{_encoded(path, safe='/-_.~')}"
        headers = sign("GET", url, query, self.credentials, datetime.now(UTC))
        encoded = "&".join(f"{_encoded(k)}={_encoded(v)}" for k, v in sorted(query.items()))
        try:
            response = self.client.get(url + (f"?{encoded}" if encoded else ""), headers=headers)
        except httpx.HTTPError as error:
            raise StoreUnavailable(f"the store could not be reached: {error}") from None
        if response.status_code >= 400:
            code = _error_code(response.content)
            raise StoreUnavailable(f"the store answered {response.status_code} {code} for {path}")
        return response

    def keys(self, prefix: str) -> Iterator[str]:
        """Every key under `prefix`, following continuation tokens."""
        token: str | None = None
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            root = ElementTree.fromstring(self._get(f"/{self.bucket}", query).content)
            for item in root.iter(f"{_NS}Contents"):
                key = item.findtext(f"{_NS}Key")
                if key:
                    yield key
            if root.findtext(f"{_NS}IsTruncated") != "true":
                return
            token = root.findtext(f"{_NS}NextContinuationToken")
            if not token:
                return

    def get(self, key: str) -> bytes:
        """The object stored under `key`."""
        return self._get(f"/{self.bucket}/{key}", {}).content


def _error_code(body: bytes) -> str:
    """The S3 error's `Code`, the one part of an error body that says what went wrong without
    echoing the request."""
    try:
        return ElementTree.fromstring(body).findtext("Code") or ""
    except ElementTree.ParseError:
        return ""

from __future__ import annotations

import base64
import json
import zlib
from typing import Any


TOKEN_PREFIX = "tp1."
MAX_COMPRESSED_BYTES = 256_000
MAX_JSON_BYTES = 2_000_000


class TokenError(ValueError):
    pass


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))
    except Exception as exc:  # noqa: BLE001
        raise TokenError("invalid base64 content token") from exc


def encode_content_token(plan: dict[str, Any]) -> str:
    raw = json.dumps(
        plan,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(raw) > MAX_JSON_BYTES:
        raise TokenError("plan is too large to encode")
    return TOKEN_PREFIX + _b64url_encode(zlib.compress(raw, level=9))


def decode_content_token(token: str) -> dict[str, Any]:
    value = (token or "").strip()
    if not value.startswith(TOKEN_PREFIX):
        raise TokenError(f"content token must start with {TOKEN_PREFIX}")
    compressed = _b64url_decode(value[len(TOKEN_PREFIX) :])
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise TokenError("compressed token exceeds size limit")
    try:
        inflater = zlib.decompressobj()
        raw = inflater.decompress(compressed, MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES or inflater.unconsumed_tail:
            raise TokenError("decoded plan exceeds size limit")
        raw += inflater.flush()
        plan = json.loads(raw.decode("utf-8"))
    except TokenError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TokenError("content token could not be decoded") from exc
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise TokenError("unsupported plan schema")
    if not plan.get("plan_id") or not isinstance(plan.get("revision"), int):
        raise TokenError("content token is missing plan identity")
    return plan


def encode_legacy_v3(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _b64url_encode(raw)

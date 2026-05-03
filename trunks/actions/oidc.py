from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from trunks.repository import Repository

from .storage import load_run

OIDC_SCHEMA = "trunks.actions.oidc_token.v1"


def mint_oidc_token(
    repo: Repository,
    *,
    run: str,
    audience: str,
    issuer: str = "https://trunks.local",
    ttl_s: int = 600,
    now_s: int | None = None,
    signing_key: str | None = None,
) -> dict[str, object]:
    key = signing_key or os.environ.get("TRUNKS_OIDC_SIGNING_KEY")
    if not key:
        raise RuntimeError("TRUNKS_OIDC_SIGNING_KEY is required to mint Trunks OIDC tokens")
    if not audience:
        raise ValueError("audience is required")
    now = int(time.time()) if now_s is None else now_s
    payload = load_run(repo, run)
    state = payload.get("state") if isinstance(payload, dict) else {}
    claims: dict[str, object] = {
        "iss": issuer,
        "sub": f"repo:{repo.name}:run:{run}",
        "aud": audience,
        "iat": now,
        "nbf": now,
        "exp": now + ttl_s,
        "repository": repo.name,
        "run_id": run,
        "commit": payload.get("commit"),
        "provider": state.get("provider") if isinstance(state, dict) else None,
        "region": state.get("region") if isinstance(state, dict) else None,
        "attempt": state.get("attempt") if isinstance(state, dict) else None,
    }
    token = _encode_jwt({"alg": "HS256", "typ": "JWT"}, claims, key.encode())
    return {
        "_schema_version": OIDC_SCHEMA,
        "object": "oidc_token",
        "run": run,
        "audience": audience,
        "issuer": issuer,
        "expires_at_s": now + ttl_s,
        "token": token,
    }


def verify_oidc_token(token: str, *, signing_key: str, audience: str | None = None, now_s: int | None = None) -> dict[str, object]:
    try:
        raw_header, raw_payload, raw_signature = token.split(".", 2)
    except ValueError as exc:
        raise ValueError("invalid JWT") from exc
    signed = f"{raw_header}.{raw_payload}".encode()
    expected = _b64url(hmac.new(signing_key.encode(), signed, hashlib.sha256).digest())
    if not hmac.compare_digest(expected, raw_signature):
        raise ValueError("invalid JWT signature")
    claims = json.loads(_b64url_decode(raw_payload).decode())
    if not isinstance(claims, dict):
        raise ValueError("invalid JWT claims")
    now = int(time.time()) if now_s is None else now_s
    if isinstance(claims.get("exp"), int) and claims["exp"] < now:
        raise ValueError("expired JWT")
    if audience is not None and claims.get("aud") != audience:
        raise ValueError("JWT audience mismatch")
    return claims


def _encode_jwt(header: dict[str, object], claims: dict[str, object], key: bytes) -> str:
    raw_header = _b64url(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
    raw_payload = _b64url(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    signed = f"{raw_header}.{raw_payload}".encode()
    signature = _b64url(hmac.new(key, signed, hashlib.sha256).digest())
    return f"{raw_header}.{raw_payload}.{signature}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)

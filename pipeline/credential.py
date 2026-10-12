"""One place that turns an Anthropic credential into request headers.

Three call sites -- the designer, the calibration agent and the classifier --
each carried a byte-identical copy of this routing. Three copies is three
chances to fix a transport bug in two of them, and the shapes are not
interchangeable: an `sk-ant-oat*` OAuth token must go in `Authorization:
Bearer` with the OAuth beta header, while an API key must go in `x-api-key`.
Sending either one the other way is a 401 that looks like a bad key.

Upstream's `author/anthropic_credential.py` has the same idea but omits the
`anthropic-beta` header, which our OAuth tokens require -- so this keeps our
behaviour and takes only the structure.
"""
from __future__ import annotations

import os

TOKEN_VAR = "ANTHROPIC_AUTH_TOKEN"
KEY_VAR = "ANTHROPIC_API_KEY"
OAUTH_PREFIX = "sk-ant-oat"
OAUTH_BETA = "oauth-2025-04-20"


def resolve_credential(env: dict[str, str] | None = None) -> str | None:
    """The credential to use, token first. Empty strings are not credentials."""
    env = os.environ if env is None else env
    return env.get(TOKEN_VAR) or env.get(KEY_VAR) or None


def auth_headers(env: dict[str, str] | None = None) -> dict[str, str]:
    """Headers for one Anthropic request, routed by credential shape."""
    credential = resolve_credential(env)
    if not credential:
        raise RuntimeError(
            f"no Anthropic credential: set {TOKEN_VAR} or {KEY_VAR} (source keys.env)")
    if credential.startswith(OAUTH_PREFIX):
        return {"Authorization": f"Bearer {credential}",
                "anthropic-beta": OAUTH_BETA}
    return {"x-api-key": credential}

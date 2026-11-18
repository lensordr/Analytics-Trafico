"""OAuth 2.1 for the Claude connector (MCP): a dispatcher signs in once and Claude gets a read-only token.

Stateless: codes and tokens are signed with the session secret (SESSION_SECRET online), so they survive restarts.
Only Claude's callback addresses may receive a code (and 127.0.0.1/localhost on your own computer, for tests).
PKCE (S256) is required. Access tokens last 1 hour, refresh tokens 30 days; a removed dispatcher loses access.
"""
import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from urllib.parse import urlsplit

CODE_SECONDS = 300
ACCESS_SECONDS = 3600
REFRESH_SECONDS = 30 * 86400
CLAUDE_CALLBACKS = {"https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback"}
SCOPE = "read"

_used_codes = {}
_lock = threading.Lock()


def redirect_allowed(uri, local):
    if uri in CLAUDE_CALLBACKS:
        return True
    parts = urlsplit(uri or "")
    return local and parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(auth, kind, payload, seconds):
    body = _b64(json.dumps({**payload, "k": kind, "e": int(time.time()) + seconds, "n": secrets.token_hex(6)},
                           separators=(",", ":")).encode())
    return f"mt.{body}.{auth._sign(kind + '.' + body)}"


def verify(auth, kind, token):
    """The payload of a valid, unexpired token of this kind (and a dispatcher who still exists), else None."""
    try:
        prefix, body, signature = str(token or "").split(".")
        if prefix != "mt" or not hmac.compare_digest(signature, auth._sign(kind + "." + body)):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if payload.get("k") != kind or payload.get("e", 0) < time.time():
        return None
    user = payload.get("u", "")
    if (auth.users and user not in auth.users) or (not auth.users and user):
        return None
    return payload


def pkce_ok(verifier, challenge):
    if not verifier or not challenge:
        return False
    digest = _b64(hashlib.sha256(verifier.encode()).digest())
    return hmac.compare_digest(digest, challenge)


def new_code(auth, user, client_id, redirect_uri, challenge):
    return sign(auth, "code", {"u": user, "c": client_id, "r": redirect_uri, "p": challenge}, CODE_SECONDS)


def tokens(auth, user):
    return {"access_token": sign(auth, "access", {"u": user}, ACCESS_SECONDS), "token_type": "Bearer",
            "expires_in": ACCESS_SECONDS, "refresh_token": sign(auth, "refresh", {"u": user}, REFRESH_SECONDS),
            "scope": SCOPE}


def exchange(auth, form):
    """Token endpoint: (status, body)."""
    grant = form.get("grant_type")
    if grant == "authorization_code":
        code = verify(auth, "code", form.get("code"))
        if not code or code["r"] != form.get("redirect_uri") or (form.get("client_id") and code["c"] != form["client_id"]):
            return 400, {"error": "invalid_grant"}
        if not pkce_ok(form.get("code_verifier", ""), code["p"]):
            return 400, {"error": "invalid_grant", "error_description": "PKCE check failed"}
        with _lock:  # each code works once
            now = time.time()
            for key in [k for k, at in _used_codes.items() if at < now]:
                del _used_codes[key]
            if code["n"] in _used_codes:
                return 400, {"error": "invalid_grant"}
            _used_codes[code["n"]] = code["e"]
        return 200, tokens(auth, code["u"])
    if grant == "refresh_token":
        refresh = verify(auth, "refresh", form.get("refresh_token"))
        if not refresh:
            return 400, {"error": "invalid_grant"}
        return 200, tokens(auth, refresh["u"])
    return 400, {"error": "unsupported_grant_type"}


def register(data, local):
    """Dynamic client registration: Claude registers itself; only its callback addresses are accepted."""
    uris = data.get("redirect_uris") if isinstance(data, dict) else None
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and redirect_allowed(u, local) for u in uris):
        return 400, {"error": "invalid_redirect_uri", "error_description": "Only Claude's connector callback is allowed."}
    return 201, {"client_id": "mt-" + secrets.token_hex(8), "redirect_uris": uris, "token_endpoint_auth_method": "none",
                 "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                 "client_name": str(data.get("client_name", "Claude"))[:80]}


def metadata(base):
    return {"issuer": base, "authorization_endpoint": f"{base}/oauth/authorize", "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register", "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"], "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"], "scopes_supported": [SCOPE]}


def resource_metadata(base):
    return {"resource": f"{base}/mcp", "authorization_servers": [base], "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"], "resource_name": "MalcomTrans"}

"""Dispatcher login for the online version: one password (APP_PASSWORD), signed session cookie.

Without APP_PASSWORD (the app on your own computer) nothing changes: no login.
"""
import hashlib
import hmac
import secrets
import threading
import time

COOKIE = "mt_session"
SESSION_DAYS = 30
MAX_FAILURES = 8          # wrong passwords per address ...
FAILURE_WINDOW = 15 * 60  # ... within 15 minutes, then wait


class Auth:
    def __init__(self, password=None, secret=None):
        self.password = password or None
        # A fixed SESSION_SECRET keeps you logged in across server restarts.
        self.secret = (secret or secrets.token_hex(32)).encode()
        self._failures = {}
        self._lock = threading.Lock()

    @property
    def enabled(self):
        return self.password is not None

    def _sign(self, value):
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    def new_cookie(self, secure):
        expires = int(time.time()) + SESSION_DAYS * 86400
        value = f"{expires}.{self._sign(str(expires))}"
        flags = f"Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")
        return f"{COOKIE}={value}; {flags}"

    @staticmethod
    def clear_cookie():
        return f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"

    def valid(self, cookie_header):
        if not self.enabled:
            return True
        for part in (cookie_header or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name != COOKIE:
                continue
            expires, _, signature = value.partition(".")
            if expires.isdigit() and int(expires) > time.time() and hmac.compare_digest(signature, self._sign(expires)):
                return True
        return False

    def blocked(self, address):
        now = time.monotonic()
        with self._lock:
            recent = [at for at in self._failures.get(address, []) if now - at < FAILURE_WINDOW]
            self._failures[address] = recent
            return len(recent) >= MAX_FAILURES

    def check(self, address, password):
        """True when the password is right; wrong attempts are counted per address."""
        if self.enabled and hmac.compare_digest(str(password).encode(), self.password.encode()):
            with self._lock:
                self._failures.pop(address, None)
            return True
        with self._lock:
            self._failures.setdefault(address, []).append(time.monotonic())
        return False

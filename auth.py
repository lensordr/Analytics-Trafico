"""Dispatcher login for the online version, signed session cookie.

Users: APP_USERS = "name1:password1;name2:password2" (each dispatcher signs in with their own name and
password, from any computer, at the same time). APP_PASSWORD alone still works: one shared password.
Without either (the app on your own computer) nothing changes: no login.
SERVICE_USERS = "service,taller": names of APP_USERS that only see ITV and the tractors' maintenance (the workshop):
they enter the km of the oil changes, nothing else. Every other user is a dispatcher.
"""
import hashlib
import hmac
import re
import secrets
import threading
import time

COOKIE = "mt_session"
SESSION_DAYS = 30
MAX_FAILURES = 8          # wrong passwords per address ...
FAILURE_WINDOW = 15 * 60  # ... within 15 minutes, then wait
MAX_TOTAL_FAILURES = 200  # from all addresses together in the same window: pause all logins
MIN_PASSWORD = 10
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,39}")


def parse_users(text):
    """'malcom1:secret one;malcom2:secret two' -> {'malcom1': 'secret one', ...} (names in lower case)."""
    users = {}
    for part in (text or "").split(";"):
        if not part.strip():
            continue
        name, sep, password = part.partition(":")
        name = name.strip().lower()
        if not sep or not NAME.fullmatch(name) or not password:
            raise ValueError("APP_USERS: write name:password pairs separated by ';' (names: letters, digits, . _ -).")
        users[name] = password
    return users


class Auth:
    def __init__(self, password=None, secret=None, users=None, service_users=()):
        self.password = password or None
        self.users = dict(users or {})
        self.service_users = {name.strip().lower() for name in service_users if name.strip()}
        # A fixed SESSION_SECRET keeps you logged in across server restarts.
        self.secret = (secret or secrets.token_hex(32)).encode()
        self._failures = {}
        self._lock = threading.Lock()

    @property
    def enabled(self):
        return self.password is not None or bool(self.users)

    @property
    def named(self):
        """True when each dispatcher signs in with a name."""
        return bool(self.users)

    def _sign(self, value):
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    def new_cookie(self, secure, user=""):
        expires = int(time.time()) + SESSION_DAYS * 86400
        payload = f"{expires}.{user}" if user else str(expires)
        value = f"{payload}.{self._sign(payload)}"
        flags = f"Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")
        return f"{COOKIE}={value}; {flags}"

    @staticmethod
    def clear_cookie():
        return f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"

    def user(self, cookie_header):
        """Signed-in user name ('' for the shared password), or None when not signed in."""
        if not self.enabled:
            return ""
        for part in (cookie_header or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name != COOKIE:
                continue
            payload, _, signature = value.rpartition(".")
            expires, _, user = payload.partition(".")
            if not (expires.isdigit() and int(expires) > time.time() and hmac.compare_digest(signature, self._sign(payload))):
                continue
            # A removed user (or a shared-password cookie once users exist) is signed out.
            if self.users and user not in self.users:
                continue
            if not self.users and user:
                continue
            return user
        return None

    def role(self, user):
        """'service' for the workshop accounts (ITV, and the maintenance km only), else 'dispatcher'."""
        return "service" if user and user in self.service_users else "dispatcher"

    def valid(self, cookie_header):
        return self.user(cookie_header) is not None

    def blocked(self, address):
        now = time.monotonic()
        with self._lock:
            for key in list(self._failures):
                self._failures[key] = [at for at in self._failures[key] if now - at < FAILURE_WINDOW]
                if not self._failures[key]:
                    del self._failures[key]
            total = sum(len(times) for times in self._failures.values())
            return len(self._failures.get(address, [])) >= MAX_FAILURES or total >= MAX_TOTAL_FAILURES

    def check(self, address, password, user=""):
        """The signed-in user name ('' for the shared password) when right, else None; wrong attempts are counted."""
        user = str(user or "").strip().lower()
        expected = self.users.get(user) if self.users else self.password
        if self.enabled and expected is not None and hmac.compare_digest(str(password).encode(), expected.encode()):
            with self._lock:
                self._failures.pop(address, None)
            return user if self.users else ""
        with self._lock:
            self._failures.setdefault(address, []).append(time.monotonic())
        return None

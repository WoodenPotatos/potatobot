import logging
import asyncio
import copy
import os
import hmac
import json
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
import discord
from cogs.utils import (
    available_languages,
    # Read-only. Nothing in this process writes the legacy file any more, so the
    # lock and the atomic-replace helpers are no longer imported: the lock
    # existed to stop two writers dropping each other's keys, and there is one
    # fewer writer than that.
    config,
    get_dashboard_locale_catalog,
    t,
)
from dotenv import load_dotenv
from flask import (Flask, jsonify, redirect, request, send_from_directory,
                   session)
from werkzeug.middleware.proxy_fix import ProxyFix

import database
import item_catalog
import permission_audit
import settings_cache
from deployment import settings as deployment_settings
from feature_access import is_enabled, update_cached_features
# Imported by name, not as a module: a route below is called
# `settings_registry` and would shadow it.
from settings_registry import (FEATURE_GROUP_ORDER, SETTING_DEFINITIONS,
                               SettingValueType, wire_json_shape)
from settings_registry import legacy_config_value as settings_registry_legacy_config_value
from version import version_display

dashboard_logger = logging.getLogger("PotatoBot.Dashboard")

# Environment variables provide deployment-specific OAuth credentials.
load_dotenv()

# Flask serves the dashboard assets directly from the repository directory.
dashboard_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")
app = Flask(__name__, static_folder=dashboard_dir, static_url_path="/")


def _load_session_secret() -> str:
    """Load a stable secret or create a private installation-local fallback."""
    configured = os.getenv("POTATOBOT_DASHBOARD_SESSION_SECRET", "").strip()
    if configured:
        if len(configured) < 32:
            raise RuntimeError(
                "POTATOBOT_DASHBOARD_SESSION_SECRET must contain at least 32 characters"
            )
        return configured

    secret_path = os.path.join(os.path.dirname(__file__), ".dashboard_session_secret")
    try:
        with open(secret_path, "r", encoding="utf-8") as secret_file:
            stored = secret_file.read().strip()
        if len(stored) >= 32:
            return stored
    except FileNotFoundError:
        pass

    generated = secrets.token_urlsafe(48)
    try:
        file_descriptor = os.open(
            secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except FileExistsError:
        with open(secret_path, "r", encoding="utf-8") as secret_file:
            stored = secret_file.read().strip()
        if len(stored) < 32:
            raise RuntimeError("Dashboard session-secret file is invalid")
        return stored
    with os.fdopen(file_descriptor, "w", encoding="utf-8") as secret_file:
        secret_file.write(generated)
    return generated


# Behind a trusted reverse proxy every request otherwise arrives from the
# loopback address, which collapses all rate limiting onto one identity. Only the
# configured number of hops is honoured so a client cannot forge the header.
if deployment_settings.trusted_proxy_hops:
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=deployment_settings.trusted_proxy_hops,
        x_proto=deployment_settings.trusted_proxy_hops,
        x_host=deployment_settings.trusted_proxy_hops,
        x_port=0,
        x_prefix=0,
    )

app.secret_key = _load_session_secret()
# A control-plane session should not outlive a working day, and the cookie needs
# an expiry of its own: without one a captured cookie stays valid for as long as
# the signing secret does. This is the absolute cap, checked against the login
# time recorded in the session, and it cannot be extended by using the page.
SESSION_LIFETIME = timedelta(hours=12)
# An unattended dashboard is the realistic exposure — a logged-in browser left
# open on an operator's desk — so the cookie itself expires after ten idle
# minutes and every authenticated request slides it forward. Flask refreshes a
# permanent cookie on each response, so the sliding window needs no bookkeeping
# of its own; only the absolute cap above does.
SESSION_IDLE_TIMEOUT = timedelta(minutes=10)

app.config.update(
    MAX_CONTENT_LENGTH=1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(
        deployment_settings.dashboard_external_url or ""
    ).lower().startswith("https://"),
    PERMANENT_SESSION_LIFETIME=SESSION_IDLE_TIMEOUT,
    SESSION_REFRESH_EACH_REQUEST=True,
)

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = deployment_settings.discord_redirect_uri
ADMIN_ID = (os.getenv("ADMIN_DISCORD_ID") or "").strip()
DISCORD_API_ENDPOINT = "https://discord.com/api/v10"
DISCORD_PERMISSION_ADMINISTRATOR = 1 << 3
DISCORD_PERMISSION_MANAGE_GUILD = 1 << 5
_rate_limit_lock = threading.Lock()
_rate_limit_events = defaultdict(deque)
_oauth_token_lock = threading.Lock()
_oauth_tokens = {}
_dashboard_bot = None

# A burst of saves should not mean a blocking Discord call each; the window is
# short enough that a revoked permission is noticed within seconds.
PERMISSION_CACHE_SECONDS = 30
# Server-held state is keyed by session id, so both maps are capped together and
# swept on write. Previously only an explicit logout ever removed an OAuth token,
# so a closed browser left a live refresh token resident for the process lifetime.
MAX_TRACKED_SESSIONS = 2048


class TtlCache:
    """A small, bounded, thread-safe cache with a fixed time to live.

    Values are deep-copied in and out so a caller mutating what it received
    cannot corrupt the cached copy; entries hold lists and dicts.
    """

    def __init__(self, ttl: float, max_entries: int):
        self._entries = {}
        self._lock = threading.Lock()
        self.ttl = ttl
        self.max_entries = max_entries

    def get(self, key: str):
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if now - stored_at > self.ttl:
                del self._entries[key]
                return None
            return copy.deepcopy(value)

    def put(self, key: str, value):
        now = time.monotonic()
        with self._lock:
            for stale in [
                existing for existing, (stored_at, _) in self._entries.items()
                if now - stored_at > self.ttl
            ]:
                del self._entries[stale]
            while len(self._entries) >= self.max_entries:
                del self._entries[next(iter(self._entries))]
            self._entries[key] = (now, copy.deepcopy(value))

    def forget(self, key: str):
        with self._lock:
            self._entries.pop(key, None)


_permission_cache = TtlCache(PERMISSION_CACHE_SECONDS, MAX_TRACKED_SESSIONS)

# Channel and role selectors are re-read on every page load, so a short cache
# keeps a standalone dashboard from making two Discord calls each time.
RESOURCE_CACHE_SECONDS = 60
_resource_cache = TtlCache(RESOURCE_CACHE_SECONDS, 256)

# Numeric Discord channel types, for the REST path where discord.py is not
# resolving them. Only the kinds the dashboard offers need naming.
DISCORD_CHANNEL_TYPES = {
    0: "text", 2: "voice", 4: "category", 5: "news",
    13: "stage_voice", 15: "forum",
}


def _forget_session(session_id: str | None):
    """Drop every piece of server-held state for one session."""
    if not session_id:
        return
    with _oauth_token_lock:
        _oauth_tokens.pop(session_id, None)
    _permission_cache.forget(session_id)


def _prune_oauth_tokens(now: float):
    """Evict tokens that can no longer be refreshed, and cap the map.

    Called on insert. A refresh token outlives the access token, so entries are
    kept for the session lifetime past expiry rather than dropped immediately.
    """
    cutoff = now - SESSION_LIFETIME.total_seconds()
    for key in [
        key for key, stored in _oauth_tokens.items()
        if stored.get("expires_at", 0) < cutoff
    ]:
        del _oauth_tokens[key]
        _permission_cache.forget(key)
    while len(_oauth_tokens) >= MAX_TRACKED_SESSIONS:
        oldest = next(iter(_oauth_tokens))
        del _oauth_tokens[oldest]
        _permission_cache.forget(oldest)


def _server_access_token(session_id: str) -> str | None:
    """Return or refresh a server-held OAuth token without putting it in cookies."""
    with _oauth_token_lock:
        stored = dict(_oauth_tokens.get(session_id, {}))
    if not stored:
        return None
    if stored.get("expires_at", 0) > time.time() + 60:
        return stored.get("access_token")
    refresh_token = stored.get("refresh_token")
    if not refresh_token or not CLIENT_ID or not CLIENT_SECRET:
        return None
    try:
        response = requests.post(
            f"{DISCORD_API_ENDPOINT}/oauth2/token",
            data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                  "grant_type": "refresh_token", "refresh_token": refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20,
        )
        response.raise_for_status()
        refreshed = response.json()
    except (requests.RequestException, ValueError):
        dashboard_logger.warning("Discord OAuth token refresh failed.")
        return None
    updated = {
        "access_token": refreshed.get("access_token"),
        "refresh_token": refreshed.get("refresh_token", refresh_token),
        "expires_at": time.time() + int(refreshed.get("expires_in", 3600)),
    }
    if not updated["access_token"]:
        return None
    with _oauth_token_lock:
        _prune_oauth_tokens(time.time())
        _oauth_tokens[session_id] = updated
    # The grant changed, so any cached permission snapshot is no longer trusted.
    _permission_cache.forget(session_id)
    return updated["access_token"]


def _within_rate_limit(bucket: str, limit: int, window: int) -> bool:
    identity = session.get("user_id") or request.remote_addr or "unknown"
    key = (bucket, str(identity))
    now = time.monotonic()
    with _rate_limit_lock:
        events = _rate_limit_events[key]
        while events and events[0] <= now - window:
            events.popleft()
        if len(events) >= limit:
            return False
        events.append(now)
        if len(_rate_limit_events) > 4096:
            for old_key in list(_rate_limit_events):
                old_events = _rate_limit_events[old_key]
                if not old_events or old_events[-1] <= now - window:
                    del _rate_limit_events[old_key]
        return True


BRAND_AVATAR_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "botdata", "pfp"
)
BRAND_AVATAR_FILE = "potatobotpfp.png"


@app.route("/brand-avatar.png")
def brand_avatar():
    """The bot's avatar, used as the sidebar mark and the browser tab icon.

    Served from `botdata/pfp/` rather than copied into `dashboard/` so the
    operator swaps one file and both surfaces follow. Cached hard because it
    changes about as often as the bot's identity does.
    """
    path = os.path.join(BRAND_AVATAR_DIR, BRAND_AVATAR_FILE)
    if not os.path.isfile(path):
        # The interface falls back to the inline potato glyph, so a missing
        # avatar is a cosmetic gap rather than a broken page.
        return ("", 404)
    return send_from_directory(BRAND_AVATAR_DIR, BRAND_AVATAR_FILE,
                               max_age=86400)


@app.route("/")
def index():
    """Serve the static shell; JavaScript fills it from the active locale."""
    return app.send_static_file("index.html")


@app.route("/api/locale")
def locale_catalog():
    """Expose display text needed before a user authenticates.

    The optional ``lang`` parameter selects the dashboard's own display language,
    which is a per-browser preference and is independent of the ``language``
    instance setting that decides what the bot says in Discord.
    """
    supported = available_languages()
    requested = request.args.get("lang", "")
    language = requested if requested in supported else (
        settings_cache.setting(None, "language")
    )
    # Only the dashboard namespace is served. The interface never reads outside
    # it, including the feature and setting labels the registry supplies, so
    # returning the whole catalogue only disclosed every bot command and
    # moderation string to anyone who could reach the page.
    catalog = get_dashboard_locale_catalog(language)
    return jsonify({
        "language": language,
        "available": supported,
        "data": {"dashboard": catalog.get("dashboard", {})},
    })


@app.route("/api/auth/login")
def login():
    """Start the Discord OAuth2 identity flow."""
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        return t("dashboard.oauth_not_configured"), 503
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    parameters = urlencode(
        {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "identify guilds",
            "state": state,
        }
    )
    url = f"https://discord.com/oauth2/authorize?{parameters}"
    return redirect(url)


@app.route("/api/callback")
def callback():
    """Exchange the OAuth code and allow only the configured administrator."""
    expected_state = session.pop("oauth_state", None)
    supplied_state = request.args.get("state", "")
    if not expected_state or not hmac.compare_digest(expected_state, supplied_state):
        session.clear()
        return t("dashboard.oauth_invalid_state"), 400
    code = request.args.get("code")
    if not code:
        return t("dashboard.oauth_missing_code"), 400

    try:
        token_response = requests.post(
            f"{DISCORD_API_ENDPOINT}/oauth2/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
    except requests.RequestException:
        dashboard_logger.exception("Discord OAuth token request failed.")
        return t("dashboard.oauth_token_error"), 400
    if token_response.status_code != 200:
        return t("dashboard.oauth_token_error"), 400

    try:
        token_data = token_response.json()
        access_token = token_data.get("access_token")
    except ValueError:
        return t("dashboard.oauth_token_error"), 400
    if not access_token:
        return t("dashboard.oauth_token_error"), 400
    try:
        user_response = requests.get(
            f"{DISCORD_API_ENDPOINT}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
    except requests.RequestException:
        dashboard_logger.exception("Discord user identity request failed.")
        return t("dashboard.oauth_user_error"), 400
    if user_response.status_code != 200:
        return t("dashboard.oauth_user_error"), 400
    try:
        user_data = user_response.json()
    except ValueError:
        return t("dashboard.oauth_user_error"), 400
    user_id = str(user_data.get("id"))
    is_host = bool(ADMIN_ID) and user_id == ADMIN_ID
    active_guild_ids = database.run_read_sync(database.get_active_guild_ids)
    if deployment_settings.profile.value == "private":
        if not is_host:
            return t("dashboard.access_denied", user_id=user_id), 403
        authorized_guild_ids = active_guild_ids
    elif is_host:
        authorized_guild_ids = active_guild_ids
    else:
        try:
            guild_response = requests.get(
                f"{DISCORD_API_ENDPOINT}/users/@me/guilds",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=20,
            )
        except requests.RequestException:
            dashboard_logger.exception("Discord user guild request failed.")
            return t("dashboard.oauth_guild_error"), 400
        if guild_response.status_code != 200:
            return t("dashboard.oauth_guild_error"), 400
        try:
            user_guilds = guild_response.json()
        except ValueError:
            return t("dashboard.oauth_guild_error"), 400
        authorized_guild_ids = {
            int(guild["id"])
            for guild in user_guilds
            if str(guild.get("id", "")).isdigit()
            and int(guild["id"]) in active_guild_ids
            and (
                guild.get("owner") is True
                or int(guild.get("permissions", "0"))
                & (DISCORD_PERMISSION_ADMINISTRATOR | DISCORD_PERMISSION_MANAGE_GUILD)
            )
        }
        if not authorized_guild_ids:
            return t("dashboard.no_authorized_guilds"), 403

    # Rotate: a fresh session on login means a pre-login cookie cannot be
    # replayed with post-login authority.
    session.clear()
    session.permanent = True
    session["logged_in"] = True
    # Only the identifier is ever used server-side; the display fields are kept
    # separately so the whole Discord user object never enters the cookie.
    session["user_id"] = user_id
    session["display"] = {
        "username": user_data.get("username") or user_id,
        "avatar": user_data.get("avatar"),
    }
    session["authorized_guild_ids"] = [str(item) for item in authorized_guild_ids]
    session["csrf_token"] = secrets.token_urlsafe(32)
    # The sliding idle window lives in the cookie's own expiry; this is what the
    # absolute cap is measured from, so refreshing the cookie cannot move it.
    session["authenticated_at"] = time.time()
    session_id = secrets.token_urlsafe(32)
    session["server_session_id"] = session_id
    with _oauth_token_lock:
        _prune_oauth_tokens(time.time())
        _oauth_tokens[session_id] = {
            "access_token": access_token,
            "refresh_token": token_data.get("refresh_token"),
            "expires_at": time.time() + int(token_data.get("expires_in", 3600)),
        }
    return redirect("/")


@app.route("/api/session/touch")
def session_touch():
    """Refresh the session cookie and nothing else.

    Navigating between dashboard pages that render from already-loaded state
    made no request at all, so `SESSION_REFRESH_EACH_REQUEST` never fired and the
    session genuinely expired while somebody was using the interface. The
    countdown was not wrong; there was nothing keeping the session alive.

    `/auth/status` would have served, but it reads the guild list and decorates it
    from the bot cache, which is far too much work to repeat on every navigation.
    This touches only the session, and returns the timeout so the client stays
    calibrated without a second call.
    """
    if session.get("logged_in") is not True:
        return unauthorized_response()
    return jsonify({
        "status": "success",
        "idle_timeout_seconds": int(SESSION_IDLE_TIMEOUT.total_seconds()),
    })


@app.route("/api/auth/status")
def auth_status():
    """Report the current session state to the static client."""
    if session.get("logged_in"):
        return jsonify(
            {
                "logged_in": True,
                "user": {
                    "id": session.get("user_id"),
                    **session.get("display", {}),
                },
                "csrf_token": session.get("csrf_token"),
                "is_host": is_host_session(),
                # Drives the topbar countdown. Any authenticated request slides
                # the cookie forward, so the client restarts it on every call.
                "idle_timeout_seconds": int(SESSION_IDLE_TIMEOUT.total_seconds()),
                # Release metadata only; the sidebar shows it next to the brand.
                "version": version_display(),
                "guilds": _decorate_guilds(
                    database.run_read_sync(
                        database.get_active_guilds,
                        session.get("authorized_guild_ids", []),
                    )
                ),
            }
        )
    return jsonify({"logged_in": False})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    _forget_session(session.get("server_session_id"))
    session.clear()
    return jsonify({"status": "success"})


@app.before_request
def enforce_absolute_session_lifetime():
    """End a session that has been alive longer than the absolute cap.

    The idle window is the cookie's own expiry and slides on every request, so
    without this a session that is merely kept warm would never end. A session
    predating this check has no recorded login instant; it is expired once
    rather than trusted indefinitely.
    """
    if session.get("logged_in") is not True:
        return None
    authenticated_at = session.get("authenticated_at")
    expired = (
        not isinstance(authenticated_at, (int, float))
        or time.time() - authenticated_at > SESSION_LIFETIME.total_seconds()
    )
    if not expired:
        return None
    _forget_session(session.get("server_session_id"))
    session.clear()
    if request.path.startswith("/api/"):
        return jsonify(
            {"status": "error", "message": t("dashboard.session_expired")}
        ), 401
    return redirect("/")


@app.before_request
def verify_csrf_token():
    """Require the session-bound token for every state-changing API request."""
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if not request.path.startswith("/api/"):
        return None
    expected = session.get("csrf_token", "")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        return jsonify(
            {"status": "error", "message": t("dashboard.csrf_invalid")}
        ), 403
    return None


@app.before_request
def apply_request_rate_limits():
    if request.path in {"/api/auth/login", "/api/callback"}:
        allowed = _within_rate_limit("auth", 10, 60)
    elif request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        allowed = _within_rate_limit("mutation", 60, 60)
    elif request.path.startswith("/api/"):
        # Reads were previously unlimited, yet each one takes a database
        # connection and some poll in a loop.
        allowed = _within_rate_limit("read", 300, 60)
    else:
        return None
    if not allowed:
        return jsonify(
            {"status": "error", "message": t("dashboard.rate_limited")}
        ), 429
    return None


def _refresh_authorized_guilds(session_id: str) -> list[str] | None:
    """Recompute a session's manageable guilds from live Discord permissions.

    Returns None when the OAuth grant can no longer be used, which the caller
    treats as an expired session.
    """
    token = _server_access_token(session_id)
    if not token:
        return None
    response = requests.get(
        f"{DISCORD_API_ENDPOINT}/users/@me/guilds",
        headers={"Authorization": f"Bearer {token}"}, timeout=10,
    )
    response.raise_for_status()
    guilds = response.json()
    active = database.run_read_sync(database.get_active_guild_ids)
    return sorted(
        str(guild["id"]) for guild in guilds
        if str(guild.get("id", "")).isdigit() and int(guild["id"]) in active
        and (guild.get("owner") is True or int(guild.get("permissions", "0"))
             & (DISCORD_PERMISSION_ADMINISTRATOR | DISCORD_PERMISSION_MANAGE_GUILD))
    )


@app.before_request
def recheck_mutation_guild_permissions():
    """Do not authorize a mutation from a stale Discord permission snapshot.

    Hosts are re-derived from ADMIN_DISCORD_ID per request and their authority is
    installation-wide, so there is no per-guild grant to refresh for them. Every
    other session is refreshed at most once per PERMISSION_CACHE_SECONDS, which
    keeps a burst of saves from making one blocking Discord call each.
    """
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if session.get("logged_in") is not True:
        return None

    if is_host_session():
        # A host's authority comes from ADMIN_DISCORD_ID, which is_host_session
        # re-derives per request, not from a Discord guild grant. Requiring a live
        # Discord call here would only make the host unable to work while Discord
        # is unreachable, so the guild list is refreshed locally instead.
        session["authorized_guild_ids"] = [
            str(item) for item in database.run_read_sync(database.get_active_guild_ids)
        ]
        return None

    session_id = session.get("server_session_id")
    if not session_id:
        session.clear()
        return unauthorized_response()

    cached = _permission_cache.get(session_id)
    if cached is not None:
        session["authorized_guild_ids"] = cached
        return None

    try:
        authorized = _refresh_authorized_guilds(session_id)
    except (requests.RequestException, ValueError):
        dashboard_logger.warning("Live Discord permission refresh failed for dashboard mutation.")
        return jsonify({"status": "error", "message": t("dashboard.permission_refresh_failed")}), 503

    if authorized is None:
        _forget_session(session_id)
        session.clear()
        return unauthorized_response()

    _permission_cache.put(session_id, authorized)
    session["authorized_guild_ids"] = authorized
    return None


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' https: data:; "
        "style-src 'self'; script-src 'self'; frame-ancestors 'none'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; "
        "object-src 'none'",
    )
    if deployment_settings.dashboard_external_url:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


def actor_id() -> int:
    """The authenticated Discord user id, the only identity the cookie carries."""
    return int(session["user_id"])


def is_host_session() -> bool:
    """Re-derive host status per request rather than trusting the cookie.

    Storing this at login meant a cookie issued before ADMIN_DISCORD_ID changed
    kept host authority, and host requests skip the live permission refresh.
    """
    if session.get("logged_in") is not True:
        return False
    stored = session.get("user_id")
    return bool(ADMIN_ID) and stored is not None and str(stored) == ADMIN_ID


def is_authorized():
    """Keep legacy instance-wide routes restricted to the configured host."""
    return session.get("logged_in") is True and is_host_session()


def is_guild_authorized(guild_id: int) -> bool:
    if session.get("logged_in") is not True:
        return False
    return str(guild_id) in session.get("authorized_guild_ids", [])


def unauthorized_response():
    return jsonify({"status": "error", "message": t("dashboard.unauthorized")}), 401


def internal_error_response(operation):
    dashboard_logger.exception("Dashboard operation failed: %s", operation)
    return jsonify({"status": "error", "message": t("dashboard.internal_error")}), 500


def _decorate_guilds(rows: list[dict]) -> list[dict]:
    """Attach the live Discord icon to stored guild metadata.

    The icon is Discord state rather than ours, so it is read from the in-process
    bot cache instead of being persisted. A dashboard running without a bot
    object simply reports no icon and the client falls back to a monogram.
    """
    for row in rows:
        guild = _dashboard_bot.get_guild(int(row["id"])) if _dashboard_bot else None
        row["icon_url"] = str(guild.icon.url) if guild and guild.icon else None
    return rows


@app.route("/api/guilds")
def guilds():
    if session.get("logged_in") is not True:
        return unauthorized_response()
    return jsonify(
        {
            "status": "success",
            "data": _decorate_guilds(
                database.run_read_sync(
                    database.get_active_guilds,
                    session.get("authorized_guild_ids", []),
                )
            ),
        }
    )


def _resources_from_bot_cache(guild_id: int) -> dict | None:
    """Channels and roles from the in-process bot, when there is one."""
    guild = _dashboard_bot.get_guild(guild_id) if _dashboard_bot else None
    if guild is None:
        return None
    return {
        # `parent_id` and `position` let the selector group channels under their
        # category in Discord's own order instead of one flat alphabetical list.
        "channels": [{"id": str(channel.id), "name": channel.name,
                      "type": str(channel.type),
                      "parent_id": (str(channel.category_id)
                                    if channel.category_id else None),
                      "position": int(getattr(channel, "position", 0))}
                     for channel in guild.channels],
        "roles": [{"id": str(role.id), "name": role.name,
                   "position": int(role.position),
                   "color": int(role.color.value),
                   "manageable": bool(guild.me and role < guild.me.top_role)}
                  for role in guild.roles if not role.is_default()],
    }


def _resources_from_discord_rest(guild_id: int) -> dict | None:
    """Channels and roles over the REST API, for a standalone dashboard process.

    This is the only route that needed the bot object, so supplying it over REST
    is what lets the dashboard be supervised as its own service. Results are
    cached briefly because the selectors are re-read on every page load.
    """
    token = (os.getenv("DISCORD_TOKEN") or "").strip()
    if not token:
        return None
    headers = {"Authorization": f"Bot {token}"}
    try:
        channels = requests.get(
            f"{DISCORD_API_ENDPOINT}/guilds/{guild_id}/channels",
            headers=headers, timeout=10,
        )
        roles = requests.get(
            f"{DISCORD_API_ENDPOINT}/guilds/{guild_id}/roles",
            headers=headers, timeout=10,
        )
        channels.raise_for_status()
        roles.raise_for_status()
        channel_rows, role_rows = channels.json(), roles.json()
    except (requests.RequestException, ValueError):
        dashboard_logger.warning(
            "Discord resource lookup failed over REST (guild_id=%s)", guild_id
        )
        return None

    # Without a bot member object the hierarchy is unknown, so fall back to the
    # managed/everyone flags Discord already reports. A role the bot cannot
    # actually assign is rejected again when it is used.
    return {
        "channels": [{"id": str(row["id"]), "name": row.get("name", ""),
                      "type": DISCORD_CHANNEL_TYPES.get(row.get("type"), "unknown"),
                      "parent_id": (str(row["parent_id"])
                                    if row.get("parent_id") else None),
                      "position": int(row.get("position") or 0)}
                     for row in channel_rows],
        "roles": [{"id": str(row["id"]), "name": row.get("name", ""),
                   "position": int(row.get("position") or 0),
                   "color": int(row.get("color") or 0),
                   "manageable": not row.get("managed", False)}
                  for row in role_rows if str(row["id"]) != str(guild_id)],
    }


@app.route("/api/guilds/<int:guild_id>/discord-resources")
def guild_discord_resources(guild_id):
    if not is_guild_authorized(guild_id):
        return unauthorized_response()

    cached = _resource_cache.get(str(guild_id))
    if cached is not None:
        return jsonify({"status": "success", "data": cached})

    data = _resources_from_bot_cache(guild_id) or _resources_from_discord_rest(guild_id)
    if data is None:
        # Neither source available: report it rather than returning empty lists
        # that render as an unexplained blank selector.
        return jsonify({"status": "error",
                        "message": t("dashboard.resources_unavailable")}), 503

    _resource_cache.put(str(guild_id), data)
    return jsonify({"status": "success", "data": data})


CHANGELOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "CHANGELOG.md"
)
# The file changes only on deployment, but it is read on every page view.
_changelog_cache = TtlCache(300, 4)


def _parse_changelog(text: str) -> list[dict]:
    """Turn the repository changelog into release sections the client can render.

    Parsing happens here rather than in the browser because the front end is
    forbidden from using a markup sink: every changelog line has to arrive as
    data and reach the DOM as a text node. A bullet wrapped over several source
    lines is rejoined, so the interface never shows half a sentence.
    """
    releases: list[dict] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("## "):
            heading = line[3:].strip()
            version, separator, label = heading.partition(" - ")
            releases.append({
                "version": version.strip() if separator else heading,
                "label": label.strip() if separator else "",
                "entries": [],
            })
        elif line.startswith("- ") and releases:
            releases[-1]["entries"].append(line[2:].strip())
        elif line.startswith("  ") and releases and releases[-1]["entries"]:
            # A continuation of the previous bullet, wrapped for the source file.
            releases[-1]["entries"][-1] += " " + line.strip()
    return releases


@app.route("/api/changelog")
def changelog():
    """Serve the deployed release notes.

    This is the changelog of the code that is actually running, read from the
    checkout rather than fetched from a repository host: the dashboard has no
    outbound allowance in its content policy, a private repository would need a
    credential, and a remote copy could describe a version this installation is
    not on.
    """
    if session.get("logged_in") is not True:
        return unauthorized_response()
    cached = _changelog_cache.get("releases")
    if cached is None:
        try:
            with open(CHANGELOG_PATH, encoding="utf-8") as handle:
                cached = _parse_changelog(handle.read())
        except OSError:
            dashboard_logger.warning("Changelog file could not be read.")
            return jsonify({"status": "error",
                            "message": t("dashboard.changelog_unavailable")}), 503
        _changelog_cache.put("releases", cached)
    return jsonify({"status": "success", "data": cached})


@app.route("/api/guilds/<int:guild_id>/features", methods=["GET", "POST"])
def guild_features(guild_id):
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    if request.method == "GET":
        return jsonify(
            {"status": "success", "data": database.run_read_sync(database.get_feature_states, guild_id)}
        )
    try:
        payload = require_json_object()
        require_exact_keys(payload, {"feature_key", "enabled", "revision"})
        require_setting_key(payload["feature_key"])
        require_bool(payload["enabled"])
        require_revision(payload["revision"])
        result = database.set_feature_state(
            guild_id=guild_id,
            feature_key=payload["feature_key"],
            enabled=payload["enabled"],
            actor_id=actor_id(),
            expected_revision=payload["revision"],
        )
        update_cached_features(guild_id, result["changes"])
        return jsonify(
            {
                "status": "success",
                "message": t("dashboard.feature_saved"),
                "data": result,
            }
        )
    except ValueError as error:
        return invalid_request_response(error)
    except database.RevisionConflictError:
        return jsonify(
            {"status": "error", "message": t("dashboard.revision_conflict")}
        ), 409
    except database.DatabaseOperationError:
        return internal_error_response("feature update")


@app.route("/api/guilds/<int:guild_id>/data-scopes", methods=["GET", "POST"])
def guild_data_scopes(guild_id):
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    if request.method == "GET":
        return jsonify(
            {"status": "success", "data": database.run_read_sync(database.get_guild_data_scopes, guild_id)}
        )
    try:
        payload = require_json_object()
        require_exact_keys(payload, {"category", "scope_type", "realm_id", "revision"})
        require_setting_key(payload["category"])
        require_setting_key(payload["scope_type"])
        require_revision(payload["revision"])
        if payload["realm_id"] is not None:
            require_nonnegative_integer(payload["realm_id"])
        result = database.set_guild_data_scope(
            guild_id=guild_id,
            category=payload["category"],
            scope_type=payload["scope_type"],
            realm_id=payload["realm_id"],
            actor_id=actor_id(),
            expected_revision=payload["revision"],
        )
        return jsonify(
            {
                "status": "success",
                "message": t("dashboard.data_scope_saved"),
                "data": result,
            }
        )
    except ValueError as error:
        return invalid_request_response(error)
    except database.RevisionConflictError:
        return jsonify(
            {"status": "error", "message": t("dashboard.revision_conflict")}
        ), 409
    except database.DatabaseOperationError:
        return internal_error_response("data-scope update")


@app.route("/api/realms", methods=["GET", "POST"])
def realms():
    if not is_authorized():
        return unauthorized_response()
    if request.method == "GET":
        return jsonify({"status": "success", "data": database.run_read_sync(database.get_realms)})
    try:
        payload = require_json_object()
        if set(payload) != {"name"}:
            raise RequestValidationError("dashboard.errors.unexpected_fields")
        realm_id = database.create_realm(
            payload["name"], actor_id()
        )
        return jsonify(
            {
                "status": "success",
                "message": t("dashboard.realm_created"),
                "data": {"realm_id": realm_id},
            }
        ), 201
    except (ValueError, sqlite3.IntegrityError) as error:
        return invalid_request_response(error)


@app.route("/api/realms/<int:realm_id>/memberships", methods=["POST"])
def request_realm_join(realm_id):
    try:
        payload = require_json_object()
        if set(payload) != {"guild_id"} or not is_guild_authorized(payload["guild_id"]):
            return unauthorized_response()
        database.request_realm_membership(realm_id, int(payload["guild_id"]))
        return jsonify(
            {"status": "success", "message": t("dashboard.realm_join_requested")}
        )
    except ValueError as error:
        return invalid_request_response(error)


@app.route(
    "/api/realms/<int:realm_id>/guilds/<int:guild_id>/approve", methods=["POST"]
)
def approve_realm_join(realm_id, guild_id):
    if not is_authorized():
        return unauthorized_response()
    try:
        database.approve_realm_membership(
            realm_id, guild_id, actor_id()
        )
        return jsonify(
            {"status": "success", "message": t("dashboard.realm_join_approved")}
        )
    except ValueError as error:
        return invalid_request_response(error)


class RequestValidationError(ValueError):
    """A rejected request that names the localized reason it was rejected.

    Raw validator text is English developer prose, so it must never reach the
    operator. Carrying a locale key instead lets each handler return a specific
    message while keeping every user-visible string in the language catalogs.
    """

    def __init__(self, locale_key: str, **params):
        super().__init__(locale_key)
        self.locale_key = locale_key
        self.params = params


def invalid_request_response(error: Exception):
    """Return 400 with a specific reason when the validator supplied one.

    Two validator layers feed this. The dashboard's own checks raise
    RequestValidationError carrying a locale key directly; database.py's checks
    raise ValidationError carrying a stable reason code, which maps to a key of
    the same name. Anything else falls back to the generic message rather than
    leaking developer prose.
    """
    if isinstance(error, RequestValidationError):
        message = t(error.locale_key, **error.params)
    elif isinstance(error, database.ValidationError):
        message = t(f"dashboard.errors.{error.reason}", **error.params)
        if message.startswith("["):
            # A reason without a translation must not surface as a raw key.
            dashboard_logger.error(
                "Validation reason has no locale key (reason=%s)", error.reason
            )
            message = t("dashboard.invalid_request")
    else:
        message = t("dashboard.invalid_request")
    return jsonify({"status": "error", "message": message}), 400


def require_json_object():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise RequestValidationError("dashboard.errors.body_not_object")
    return payload


def require_exact_keys(payload: dict, keys: set, optional: set = frozenset()):
    """Reject a body that omits a required field or carries an unknown one.

    `optional` exists so a route can grow a field without every existing client
    having to send it, while an unknown field is still refused rather than
    silently ignored.
    """
    supplied = set(payload)
    if not keys <= supplied or not supplied <= (keys | set(optional)):
        raise RequestValidationError("dashboard.errors.unexpected_fields")
    return payload


def require_nonnegative_integer(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RequestValidationError("dashboard.errors.not_nonnegative_integer")
    return value


def require_revision(value):
    """Validate an optimistic revision before it reaches ``int()`` in the model.

    A non-integer revision used to raise TypeError deep in ``database.py`` and
    escape the handlers as an unhandled 500.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RequestValidationError("dashboard.errors.revision_invalid")
    return value


def require_setting_key(value):
    """A key must be hashable and a string before it indexes a registry dict."""
    if not isinstance(value, str) or not value:
        raise RequestValidationError("dashboard.errors.key_invalid")
    return value


def require_bool(value):
    if not isinstance(value, bool):
        raise RequestValidationError("dashboard.errors.not_boolean")
    return value


def _legacy_config_value(definition, guild_id: int):
    if definition.key.startswith("shop_price_"):
        item_key = definition.key.removeprefix("shop_price_")
        return database.run_read_sync(
            database.get_shop_price, guild_id, item_key, definition.default
        )
    if definition.key.startswith("reward_"):
        remainder = definition.key.removeprefix("reward_")
        activity, reward_type = remainder.rsplit("_", 1)
        coin, xp = database.run_read_sync(
            database.get_reward, guild_id, activity,
            definition.default, definition.default,
        )
        return coin if reward_type == "coin" else xp
    # `config.json` is a read-only fallback now: nothing writes it, and it only
    # answers for a setting an installation has never saved. One copy of that
    # walk, shared with the runtime resolver and the permission audit.
    return settings_registry_legacy_config_value(definition, config)


def _mirror_price_and_reward_tables(guild_id: int, changed: dict):
    """Write this guild's own price and reward overrides.

    Each statement upserts a row keyed by this guild, so the installation defaults
    at guild_id 0 stay intact and one guild's edit can never change another's
    prices — the hazard that existed while these tables had no guild dimension.
    """
    guild_id = int(guild_id)
    with database.get_connection() as conn:
        for key, value in changed.items():
            if key.startswith("shop_price_"):
                conn.execute(
                    "INSERT INTO shop_prices (guild_id, item_id, price) "
                    "VALUES (?, ?, ?) ON CONFLICT(guild_id, item_id) "
                    "DO UPDATE SET price = excluded.price",
                    (guild_id, key.removeprefix("shop_price_"), value),
                )
            elif key.startswith("reward_"):
                activity, reward_type = key.removeprefix("reward_").rsplit("_", 1)
                column = "coin_reward" if reward_type == "coin" else "xp_reward"
                # A new override row must start from the installation default, so
                # the sibling column keeps its value instead of becoming NULL.
                # Read it on this connection rather than opening a nested one.
                default = conn.execute(
                    "SELECT coin_reward, xp_reward FROM rewards "
                    "WHERE guild_id = 0 AND activity_id = ?", (activity,)
                ).fetchone()
                default_coin, default_xp = default if default else (0, 0)
                conn.execute(
                    f"INSERT INTO rewards "
                    f"(guild_id, activity_id, coin_reward, xp_reward) "
                    f"VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, activity_id) "
                    f"DO UPDATE SET {column} = excluded.{column}",
                    (
                        guild_id, activity,
                        value if column == "coin_reward" else default_coin,
                        value if column == "xp_reward" else default_xp,
                    ),
                )


@app.route("/api/settings/registry")
def settings_registry():
    if session.get("logged_in") is not True:
        return unauthorized_response()
    return jsonify({
        "status": "success",
        "data": {
            key: definition.public_dict()
            for key, definition in SETTING_DEFINITIONS.items()
            if not definition.sensitive
        },
        # The switcher groups features by the registry's declared group, so the
        # render order has to come from the registry too rather than being a
        # second list kept in step by hand in JavaScript.
        "feature_group_order": list(FEATURE_GROUP_ORDER),
    })


@app.route("/api/item-catalog")
def item_catalog_registry():
    """Serve the shared built-in item catalog to the shop and gacha editors.

    Without it an operator has to already know which item keys a consumable
    template or a banner reward may name, and the interface would carry a second
    copy of a list that only `item_catalog` is allowed to define. It holds no
    secret and no Discord state, and every guild sees the same built-ins, so it
    needs a session but no guild scope.
    """
    if session.get("logged_in") is not True:
        return unauthorized_response()
    return jsonify({"status": "success", "data": item_catalog.catalog_payload()})


def _snowflake_arg(value) -> int:
    """One Discord id from a request body, as an integer or a decimal string."""
    if isinstance(value, bool):
        raise ValueError("not a snowflake")
    if isinstance(value, str):
        if not value.isdigit():
            raise ValueError("not a snowflake")
        value = int(value)
    if not isinstance(value, int) or value <= 0:
        raise ValueError("not a snowflake")
    return value


_SNOWFLAKE_VALUE_TYPES = {
    SettingValueType.CHANNEL, SettingValueType.ROLE,
    SettingValueType.CHANNEL_LIST, SettingValueType.ROLE_LIST,
}


def _wire_value(definition, value):
    """Render a setting for the browser, with snowflakes as strings.

    A Discord id is 64-bit and a JavaScript number holds 53 bits exactly, so
    `JSON.parse` turns 1420070400000000001 into ...200 — the id then matches no
    channel and the selector shows it as unavailable, and saving writes the
    rounded value back. Discord's own API sends every snowflake as a string for
    this reason; so does this one. Storage is unaffected: the values stay
    integers in `guild_settings` and in `config.json`.
    """
    # A JSON setting can carry ids inside it, and they round exactly the same
    # way — the bug does not care how deep the snowflake sits. Where each shape
    # holds one is declared in the registry, so this and the validator cannot
    # disagree about it.
    if definition.json_shape:
        return wire_json_shape(definition.json_shape, value)
    if definition.value_type not in _SNOWFLAKE_VALUE_TYPES:
        return value
    if isinstance(value, list):
        return [str(item) for item in value]
    return None if value is None else str(value)


@app.route("/api/guilds/<int:guild_id>/settings", methods=["GET", "PATCH"])
def guild_settings(guild_id):
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    try:
        if request.method == "GET":
            stored = database.run_read_sync(database.get_guild_settings, guild_id)
            data = {}
            for key, definition in SETTING_DEFINITIONS.items():
                if definition.sensitive:
                    continue
                row = stored.get(key)
                row = row or {
                    "value": _legacy_config_value(definition, guild_id),
                    "revision": 0,
                }
                data[key] = {**row,
                             "value": _wire_value(definition, row["value"])}
            return jsonify({"status": "success", "data": data})
        payload = require_json_object()
        require_exact_keys(payload, {"changes"})
        if not isinstance(payload["changes"], list) or not payload["changes"]:
            raise RequestValidationError("dashboard.errors.changes_invalid")
        for change in payload["changes"]:
            if not isinstance(change, dict) or set(change) != {"key", "value", "revision"}:
                raise RequestValidationError("dashboard.errors.changes_invalid")
            require_setting_key(change["key"])
            require_revision(change["revision"])
        result = database.set_guild_settings(
            guild_id, actor_id(), payload["changes"]
        )
        # Same-process visibility, the way a feature change already works: the
        # revision poll converges two processes, but waiting two seconds for a
        # save the operator just made is not "live".
        settings_cache.apply_changes(guild_id, result)
        # The price and reward tables are separate per-guild rows that the shop
        # and the reward paths read directly, so they still have to be written.
        # `config.json` no longer is: nothing writes it any more.
        if not app.config.get("TESTING"):
            _mirror_price_and_reward_tables(
                guild_id, {key: row["value"] for key, row in result.items()}
            )
        # The response re-seeds the client's copy, so it has to be wired the same
        # way the GET is or the next comparison sees a change that is not one.
        wired = {key: {**row,
                       "value": _wire_value(SETTING_DEFINITIONS[key], row["value"])}
                 for key, row in result.items()}
        return jsonify({"status": "success", "message": t("dashboard.settings_saved"),
                        "data": wired})
    except ValueError as error:
        return invalid_request_response(error)
    except database.RevisionConflictError:
        return jsonify({"status": "error", "message": t("dashboard.revision_conflict")}), 409
    except database.DatabaseOperationError:
        return internal_error_response("guild settings")


@app.route("/api/guilds/<int:guild_id>/permissions")
def guild_permission_report(guild_id):
    """Run the same permission diagnostic `/checkperms` runs, for this guild.

    It needs the live guild — channel overwrites and role hierarchy are Discord's
    state, not ours — so it is only available when the dashboard shares a process
    with the bot. A standalone dashboard says so instead of reporting a clean
    result it could not actually check.
    """
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    guild = _dashboard_bot.get_guild(guild_id) if _dashboard_bot else None
    if guild is None or guild.me is None:
        return jsonify({"status": "error",
                        "message": t("dashboard.resources_unavailable")}), 503
    report = permission_audit.build_report(
        guild,
        database.run_read_sync(database.get_feature_states, guild_id),
        permission_audit.resolved_settings(
            database.run_read_sync(database.get_guild_settings, guild_id),
            config,
        ),
    )
    return jsonify({"status": "success", "data": report.as_dict()})


@app.route("/api/guilds/<int:guild_id>/audit")
def guild_audit(guild_id):
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    return jsonify({"status": "success", "data": database.run_read_sync(database.get_settings_audit, guild_id)})


@app.route("/api/guilds/<int:guild_id>/privacy/erasures", methods=["POST"])
def guild_privacy_erasures(guild_id):
    """Erase one member on their behalf, for a request received out of band.

    Host only, not guild-administrator: erasure spans the whole installation
    because wallets are keyed by user id alone, so no single guild's administrator
    can authorize it. The work itself is queued for the bot, which is the only
    process that can withdraw a Discord grant.
    """
    if not is_authorized():
        return unauthorized_response()
    try:
        payload = require_json_object()
        require_exact_keys(payload, {"user_id", "confirm"})
        if require_bool(payload["confirm"]) is not True:
            raise RequestValidationError("dashboard.errors.confirmation_required")
        subject = str(payload["user_id"]).strip()
        if not subject.isdigit() or int(subject) <= 0:
            raise RequestValidationError("dashboard.errors.erasure_subject_invalid")
        action_id = database.queue_control_action(
            guild_id, actor_id(), "erase_member", {"user_id": int(subject)}
        )
        dashboard_logger.info(
            "Queued an operator-requested erasure (action_id=%s).", action_id
        )
        return jsonify({
            "status": "success",
            "message": t("dashboard.erasure_queued"),
            "data": {"action_id": action_id},
        })
    except ValueError as error:
        return invalid_request_response(error)
    except database.DatabaseOperationError:
        return internal_error_response("member erasure")


def _validate_work_response(payload: dict) -> dict:
    """Screen one `/work` response before the model layer sees it.

    Types are checked here so a wrong one cannot reach an `int()` in
    database.py and escape as an unhandled 500; the range, length and tier
    rules stay in the model, which is the layer the bot also goes through.
    """
    if not isinstance(payload.get("tier"), str):
        raise RequestValidationError("dashboard.errors.work_tier_invalid")
    if not isinstance(payload.get("message"), str):
        raise RequestValidationError(
            "dashboard.errors.work_message_invalid",
            limit=database.WORK_MESSAGE_MAX_LENGTH,
        )
    weight = payload.get("weight", 1)
    if isinstance(weight, bool) or not isinstance(weight, int):
        raise RequestValidationError("dashboard.errors.work_weight_invalid")
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RequestValidationError("dashboard.errors.work_enabled_invalid")
    return {"tier": payload["tier"], "message": payload["message"],
            "weight": weight, "enabled": enabled}


@app.route("/api/guilds/<int:guild_id>/work-responses", methods=["GET", "POST"])
def guild_work_responses(guild_id):
    """Per-guild `/work` response pool.

    A guild with no rows is not an error: the command falls back to the shipped
    locale responses per tier, so a guild can override the flavour text of one
    outcome without having to author all three.
    """
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    if request.method == "GET":
        return jsonify({"status": "success", "data": {
            "responses": database.run_read_sync(
                database.get_work_responses, guild_id
            ),
            "tiers": list(database.WORK_TIERS),
            "message_max_length": database.WORK_MESSAGE_MAX_LENGTH,
            "per_tier_limit": database.WORK_RESPONSES_PER_TIER,
            "earnings_placeholder": database.WORK_EARNINGS_PLACEHOLDER,
        }})
    try:
        payload = require_json_object()
        require_exact_keys(payload, {"tier", "message"},
                           optional={"weight", "enabled"})
        fields = _validate_work_response(payload)
        result = database.create_work_response(
            guild_id, actor_id(), fields["tier"], fields["message"],
            fields["weight"], fields["enabled"],
        )
        return jsonify({"status": "success",
                        "message": t("dashboard.work_response_created"),
                        "data": result}), 201
    except ValueError as error:
        return invalid_request_response(error)
    except database.DatabaseOperationError:
        return internal_error_response("work response creation")


@app.route("/api/guilds/<int:guild_id>/work-responses/<int:response_id>",
           methods=["PATCH", "DELETE"])
def modify_guild_work_response(guild_id, response_id):
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    try:
        payload = require_json_object()
        if request.method == "DELETE":
            require_exact_keys(payload, {"revision"})
            require_revision(payload["revision"])
            database.delete_work_response(
                guild_id, actor_id(), response_id, payload["revision"]
            )
            return jsonify({"status": "success",
                            "message": t("dashboard.work_response_deleted")})
        require_exact_keys(payload, {"tier", "message", "weight", "enabled",
                                     "revision"})
        require_revision(payload["revision"])
        fields = _validate_work_response(payload)
        result = database.update_work_response(
            guild_id, actor_id(), response_id, fields["tier"],
            fields["message"], fields["weight"], fields["enabled"],
            payload["revision"],
        )
        return jsonify({"status": "success",
                        "message": t("dashboard.work_response_updated"),
                        "data": result})
    except LookupError:
        return jsonify({"status": "error",
                        "message": t("dashboard.errors.work_response_not_found")}), 404
    except ValueError as error:
        return invalid_request_response(error)
    except database.RevisionConflictError:
        return jsonify({"status": "error",
                        "message": t("dashboard.revision_conflict")}), 409
    except database.DatabaseOperationError:
        return internal_error_response("work response update")


@app.route("/api/guilds/<int:guild_id>/gacha", methods=["GET", "PATCH"])
def guild_gacha(guild_id):
    """Read every banner a guild has, or save one of them.

    The list is what the interface renders, so a guild running several banners
    does not need one request per banner to draw its picker. A PATCH names the
    banner it edits and defaults to the installation default, which is what a
    single-banner guild has always been editing.
    """
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    if request.method == "GET":
        return jsonify({"status": "success", "data": database.run_read_sync(
            database.list_gacha_banners, guild_id)})
    try:
        payload = require_json_object()
        require_exact_keys(
            payload, {"enabled", "config", "revision"},
            optional={"banner_key", "display_name"},
        )
        require_bool(payload["enabled"])
        require_revision(payload["revision"])
        if not isinstance(payload["config"], dict):
            raise RequestValidationError("dashboard.errors.config_not_object")
        banner_key = payload.get("banner_key", database.DEFAULT_GACHA_BANNER_KEY)
        if not isinstance(banner_key, str):
            raise RequestValidationError("dashboard.errors.banner_key_invalid")
        display_name = payload.get("display_name")
        if display_name is not None and not isinstance(display_name, str):
            raise RequestValidationError("dashboard.errors.banner_name_invalid")
        result = database.set_gacha_banner(
            guild_id, actor_id(), payload["enabled"],
            payload["config"], payload["revision"],
            banner_key=banner_key, display_name=display_name,
        )
        return jsonify({"status": "success", "message": t("dashboard.gacha_saved"),
                        "data": result})
    except ValueError as error:
        return invalid_request_response(error)
    except database.RevisionConflictError:
        return jsonify({"status": "error", "message": t("dashboard.revision_conflict")}), 409
    except database.DatabaseOperationError:
        return internal_error_response("gacha update")


@app.route("/api/guilds/<int:guild_id>/gacha/banners", methods=["POST"])
def create_guild_gacha_banner(guild_id):
    """Add a banner. It starts disabled so a half-filled reward table is never
    pullable, and it copies the installation default as its starting point so an
    operator edits a valid configuration rather than authoring one."""
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    try:
        payload = require_json_object()
        require_exact_keys(payload, {"banner_key", "display_name"},
                           optional={"config"})
        if not isinstance(payload["banner_key"], str):
            raise RequestValidationError("dashboard.errors.banner_key_invalid")
        if not isinstance(payload["display_name"], str):
            raise RequestValidationError("dashboard.errors.banner_name_invalid")
        config_value = payload.get("config")
        if config_value is None:
            config_value = json.loads(json.dumps(database.DEFAULT_GACHA_CONFIG))
        elif not isinstance(config_value, dict):
            raise RequestValidationError("dashboard.errors.config_not_object")
        result = database.create_gacha_banner(
            guild_id, actor_id(), payload["banner_key"],
            payload["display_name"], config_value,
        )
        return jsonify({"status": "success",
                        "message": t("dashboard.gacha_banner_created"),
                        "data": result}), 201
    except ValueError as error:
        return invalid_request_response(error)
    except database.DatabaseOperationError:
        return internal_error_response("gacha banner creation")


@app.route("/api/guilds/<int:guild_id>/gacha/banners/<banner_key>",
           methods=["DELETE"])
def delete_guild_gacha_banner(guild_id, banner_key):
    """Delete one banner. Its pull history and pity counters survive, because
    pull rows are immutable and a member paid for the pity they hold."""
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    try:
        payload = require_json_object()
        require_exact_keys(payload, {"revision"})
        require_revision(payload["revision"])
        result = database.delete_gacha_banner(
            guild_id, actor_id(), banner_key, payload["revision"]
        )
        return jsonify({"status": "success",
                        "message": t("dashboard.gacha_banner_deleted"),
                        "data": result})
    except ValueError as error:
        return invalid_request_response(error)
    except database.RevisionConflictError:
        return jsonify({"status": "error", "message": t("dashboard.revision_conflict")}), 409
    except database.DatabaseOperationError:
        return internal_error_response("gacha banner deletion")


SAFE_SHOP_TEMPLATES = {
    "fixed_role", "timed_role", "vault", "consumable", "coin_bundle",
    "fulfillment_voucher",
}

# A Discord select menu holds 25 options and the shop renders every built-in
# item plus every enabled custom one into one menu, so the cap is derived rather
# than chosen: adding a built-in item must lower it automatically, or a guild at
# the old cap would push the menu past the limit and take `/shop` down entirely.
SHOP_ITEM_LIMIT = 25 - len(database.BUILTIN_SHOP_KEYS)
# Long enough for any snowflake, short enough that the column cannot be abused.
DISCORD_ID_MAX_LENGTH = 24


@app.route("/api/guilds/<int:guild_id>/shop-items", methods=["GET"])
def guild_shop_items(guild_id):
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    return jsonify({"status": "success",
                    "data": database.run_read_sync(database.get_shop_item_definitions, guild_id)})


def _validate_shop_item(payload: dict, *, require_key: bool):
    """Validate a custom shop item for creation or for an edit.

    Editing reuses this so an approved template can never be widened by going
    through the update path instead of the create path.
    """
    fields = {"template_type", "enabled", "price", "config", "hu"}
    if require_key:
        fields.add("item_key")
    if set(payload) != fields or payload["template_type"] not in SAFE_SHOP_TEMPLATES:
        raise RequestValidationError("dashboard.errors.shop_template_invalid")

    key = None
    if require_key:
        key = payload["item_key"]
        if not isinstance(key, str) or not key.replace("_", "").isalnum() or len(key) > 64:
            raise RequestValidationError("dashboard.errors.shop_key_invalid")
        if key in database.BUILTIN_SHOP_KEYS:
            # A custom row with a built-in key would replace that item's purchase
            # handler and price in the live shop menu.
            raise RequestValidationError("dashboard.errors.shop_key_reserved")

    price = require_nonnegative_integer(payload["price"])
    if not isinstance(payload["enabled"], bool) or not isinstance(payload["config"], dict):
        raise RequestValidationError("dashboard.errors.shop_item_invalid")
    item_config = payload["config"]
    template = payload["template_type"]
    if template == "fixed_role" and (
        set(item_config) != {"role_id"} or not isinstance(item_config["role_id"], int)
    ):
        raise RequestValidationError("dashboard.errors.shop_config_fixed_role")
    if template == "timed_role" and (
        set(item_config) != {"role_id", "duration_days"}
        or not isinstance(item_config["role_id"], int)
        or not isinstance(item_config["duration_days"], int)
        or not 1 <= item_config["duration_days"] <= 3650
    ):
        raise RequestValidationError("dashboard.errors.shop_config_timed_role")
    if template == "vault" and (
        set(item_config) != {"amount"} or not isinstance(item_config["amount"], int)
        or item_config["amount"] <= 0
    ):
        raise RequestValidationError("dashboard.errors.shop_config_vault")
    # Validated against the shared catalog so the API and the interface's item
    # picker can never disagree about which consumables exist.
    if template == "consumable" and (
        set(item_config) != {"item_key"}
        or item_config["item_key"] not in item_catalog.INVENTORY_ITEM_KEYS
    ):
        raise RequestValidationError("dashboard.errors.shop_config_consumable")
    if template == "fulfillment_voucher" and (
        set(item_config) != {"asset_type", "duration_days"}
        or item_config["asset_type"] not in {"emoji", "sticker", "sound"}
        or not isinstance(item_config["duration_days"], int)
        or not 1 <= item_config["duration_days"] <= 3650
    ):
        raise RequestValidationError("dashboard.errors.shop_config_voucher")
    hu = payload["hu"]
    if not isinstance(hu, dict) or set(hu) != {"name", "description"} or not all(
        isinstance(hu[field], str) and hu[field].strip() for field in hu
    ):
        raise RequestValidationError("dashboard.errors.shop_localization_required")
    if template == "coin_bundle":
        if set(item_config) != {"amount", "repeatable"} or not isinstance(item_config.get("repeatable"), bool):
            raise RequestValidationError("dashboard.errors.shop_config_coin_bundle")
        amount = item_config.get("amount")
        repeatable = item_config["repeatable"]
        if not isinstance(amount, int) or amount < 0 or (repeatable and amount > price):
            raise RequestValidationError("dashboard.errors.shop_coin_bundle_inflationary")
    return {
        "item_key": key,
        "template_type": template,
        "enabled": payload["enabled"],
        "price": price,
        "config": item_config,
        "hu": {"name": hu["name"].strip(), "description": hu["description"].strip()},
    }


@app.route("/api/guilds/<int:guild_id>/shop-items", methods=["POST"])
def create_guild_shop_item(guild_id):
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    try:
        item = _validate_shop_item(require_json_object(), require_key=True)
        key, price, hu = item["item_key"], item["price"], item["hu"]
        payload = item
        timestamp = datetime.now(timezone.utc).isoformat()
        with database.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Every stored row counts, not just the enabled ones. Counting only
            # enabled rows let a guild accumulate disabled definitions and then
            # re-enable past what a Discord select menu can display.
            if conn.execute(
                "SELECT COUNT(*) FROM shop_item_definitions WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()[0] >= SHOP_ITEM_LIMIT:
                raise RequestValidationError("dashboard.errors.shop_item_limit", limit=SHOP_ITEM_LIMIT)
            conn.execute(
                "INSERT INTO shop_item_definitions "
                "(guild_id, item_key, template_type, enabled, price, config_json, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, key, payload["template_type"], int(payload["enabled"]), price,
                 json.dumps(payload["config"], sort_keys=True), actor_id(), timestamp),
            )
            conn.execute(
                "INSERT INTO shop_item_localizations "
                "(guild_id, item_key, language, name, description) VALUES (?, ?, 'hu', ?, ?)",
                (guild_id, key, hu["name"].strip(), hu["description"].strip()),
            )
            # Written on the same connection so the item and its audit row
            # commit together; a separate write could leave the item unaudited.
            database.write_settings_audit(
                conn, guild_id, actor_id(),
                "shop_item.create", key, None, payload,
            )
        return jsonify({"status": "success", "message": t("dashboard.shop_item_created")}), 201
    except (ValueError, sqlite3.IntegrityError) as error:
        return invalid_request_response(error)


@app.route("/api/guilds/<int:guild_id>/shop-items/<item_key>", methods=["PATCH", "DELETE"])
def modify_guild_shop_item(guild_id, item_key):
    """Edit or remove one custom item. Built-in items are not addressable here.

    The stable item key is immutable: an edit that could rename it would break
    every inventory row and purchase record that already refers to it.
    """
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    if item_key in database.BUILTIN_SHOP_KEYS:
        return jsonify({"status": "error",
                        "message": t("dashboard.errors.shop_key_reserved")}), 400
    try:
        payload = require_json_object()
        if request.method == "DELETE":
            require_exact_keys(payload, {"revision"})
            result = database.delete_shop_item_definition(
                guild_id, actor_id(), item_key, require_revision(payload["revision"])
            )
            return jsonify({"status": "success",
                            "message": t("dashboard.shop_item_deleted"), "data": result})

        revision = require_revision(payload.pop("revision", None))
        item = _validate_shop_item(payload, require_key=False)
        result = database.update_shop_item_definition(
            guild_id, actor_id(), item_key, item, revision
        )
        return jsonify({"status": "success",
                        "message": t("dashboard.shop_item_updated"), "data": result})
    except LookupError:
        return jsonify({"status": "error",
                        "message": t("dashboard.errors.shop_item_not_found")}), 404
    except ValueError as error:
        return invalid_request_response(error)
    except database.RevisionConflictError:
        return jsonify({"status": "error", "message": t("dashboard.revision_conflict")}), 409
    except database.DatabaseOperationError:
        return internal_error_response("shop item update")


def _validate_builder_content(document_type: str, content: dict):
    if not isinstance(content, dict):
        raise RequestValidationError("dashboard.errors.builder_content_object")
    if document_type == "embed":
        if set(content) - {"title", "description", "color", "footer", "fields"}:
            raise RequestValidationError("dashboard.errors.builder_embed_fields_unsupported")
        if len(str(content.get("title", ""))) > 256 or len(str(content.get("description", ""))) > 4096:
            raise RequestValidationError("dashboard.errors.builder_embed_too_long")
        fields = content.get("fields", [])
        if not isinstance(fields, list) or len(fields) > 25:
            raise RequestValidationError("dashboard.errors.builder_embed_field_invalid")
        # The worker calls field.get(...) per entry, so a list of anything else
        # would only fail after the action was already queued.
        for field in fields:
            if not isinstance(field, dict) or set(field) - {"name", "value", "inline"}:
                raise RequestValidationError("dashboard.errors.builder_embed_field_invalid")
        if "color" in content:
            color = content["color"]
            if isinstance(color, bool) or not isinstance(color, int) or not 0 <= color <= 0xFFFFFF:
                raise RequestValidationError("dashboard.errors.builder_embed_color_invalid")
    elif document_type == "rules":
        if set(content) != {"sections"} or not isinstance(content["sections"], list):
            raise RequestValidationError("dashboard.errors.builder_rules_invalid")
        if len(content["sections"]) > 25:
            raise RequestValidationError("dashboard.errors.builder_rules_too_many")
        for section in content["sections"]:
            if not isinstance(section, dict):
                raise RequestValidationError("dashboard.errors.builder_rules_invalid")
    elif document_type == "panel":
        if set(content) - {"panel_type", "title", "description", "options"}:
            raise RequestValidationError("dashboard.errors.builder_panel_invalid")
        if content.get("panel_type") not in {"tickets", "airlock", "role_menu"}:
            raise RequestValidationError("dashboard.errors.builder_panel_type")
    else:
        raise RequestValidationError("dashboard.errors.builder_type_unsupported")
    return content


@app.route("/api/guilds/<int:guild_id>/builders", methods=["GET", "POST"])
def guild_builders(guild_id):
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    if request.method == "GET":
        return jsonify({"status": "success", "data": database.run_read_sync(database.list_dashboard_documents, guild_id)})
    try:
        payload = require_json_object()
        require_exact_keys(payload, {"document_type", "name", "content", "revision"})
        require_setting_key(payload["document_type"])
        require_revision(payload["revision"])
        if not isinstance(payload["name"], str) or not payload["name"].strip():
            raise RequestValidationError("dashboard.errors.builder_name_required")
        content = _validate_builder_content(payload["document_type"], payload["content"])
        result = database.save_dashboard_document(
            guild_id, actor_id(), payload["document_type"],
            payload["name"], content, payload["revision"],
        )
        return jsonify({"status": "success", "message": t("dashboard.draft_saved"),
                        "data": result}), 201
    except ValueError as error:
        return invalid_request_response(error)
    except database.RevisionConflictError:
        return jsonify({"status": "error", "message": t("dashboard.revision_conflict")}), 409
    except database.DatabaseOperationError:
        return internal_error_response("builder save")


@app.route("/api/guilds/<int:guild_id>/builders/<int:document_id>", methods=["DELETE"])
def delete_guild_builder(guild_id, document_id):
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    try:
        payload = require_json_object()
        require_exact_keys(payload, {"revision"})
        result = database.delete_dashboard_document(
            guild_id, actor_id(), document_id, require_revision(payload["revision"])
        )
        return jsonify({"status": "success",
                        "message": t("dashboard.draft_deleted"), "data": result})
    except LookupError:
        return jsonify({"status": "error",
                        "message": t("dashboard.errors.document_not_found")}), 404
    except ValueError as error:
        return invalid_request_response(error)
    except database.RevisionConflictError:
        return jsonify({"status": "error", "message": t("dashboard.revision_conflict")}), 409
    except database.DatabaseOperationError:
        return internal_error_response("builder delete")


@app.route("/api/guilds/<int:guild_id>/builders/<int:document_id>/publish", methods=["POST"])
def publish_builder(guild_id, document_id):
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    try:
        payload = require_json_object()
        if set(payload) != {"channel_id"}:
            raise RequestValidationError("dashboard.errors.publish_target_invalid")
        try:
            # A string, for the same 53-bit reason the settings route accepts one.
            payload["channel_id"] = _snowflake_arg(payload["channel_id"])
        except ValueError:
            raise RequestValidationError("dashboard.errors.publish_target_invalid")
        # Verify the channel belongs to this guild before anything is queued.
        # Leaving it to the worker meant arbitrary snowflakes entered the outbox
        # and failed later as an opaque error code the operator never sees.
        guild = _dashboard_bot.get_guild(guild_id) if _dashboard_bot else None
        if guild is not None and guild.get_channel(payload["channel_id"]) is None:
            raise RequestValidationError("dashboard.errors.publish_channel_not_in_guild")

        documents = {item["document_id"]: item for item in database.run_read_sync(database.list_dashboard_documents, guild_id)}
        document = documents.get(document_id)
        if not document:
            raise RequestValidationError("dashboard.errors.document_not_found")
        action_type = {"embed": "send_embed", "rules": "publish_rules",
                       "panel": "publish_panel"}[document["document_type"]]
        action_id = database.queue_control_action(
            guild_id, actor_id(), action_type,
            {"document_id": document_id, "channel_id": payload["channel_id"]},
        )
        return jsonify({"status": "success", "message": t("dashboard.action_queued"),
                        "data": {"action_id": action_id}}), 202
    except ValueError as error:
        return invalid_request_response(error)


@app.route("/api/guilds/<int:guild_id>/actions/<int:action_id>")
def guild_action_status(guild_id, action_id):
    """Report a queued publish's progress so the client can stop guessing.

    The 202 from the publish route already returns an action_id; without this the
    outcome was only visible in a database column no interface ever read.
    """
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    action = database.run_read_sync(database.get_control_action, guild_id, action_id)
    if action is None:
        return jsonify({"status": "error",
                        "message": t("dashboard.errors.action_not_found")}), 404
    return jsonify({"status": "success", "data": action})


@app.route("/api/guilds/<int:guild_id>/fulfillment", methods=["GET"])
def guild_fulfillment(guild_id):
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    return jsonify({"status": "success", "data": database.run_read_sync(database.get_fulfillment_requests, guild_id)})


@app.route("/api/guilds/<int:guild_id>/fulfillment/<int:request_id>", methods=["POST"])
def complete_guild_fulfillment(guild_id, request_id):
    if not is_guild_authorized(guild_id):
        return unauthorized_response()
    try:
        payload = require_json_object()
        require_exact_keys(payload, {"discord_item_id"})
        discord_item_id = str(payload["discord_item_id"])
        if not discord_item_id.isdigit():
            raise RequestValidationError("dashboard.errors.discord_item_id_invalid")
        if len(discord_item_id) > DISCORD_ID_MAX_LENGTH:
            raise RequestValidationError("dashboard.errors.discord_item_id_too_long")
        result = database.fulfill_voucher_request(
            guild_id, request_id, actor_id(), discord_item_id
        )
        if not result["fulfilled"]:
            return jsonify({"status": "error", "message": t("dashboard.fulfillment_unavailable")}), 409
        return jsonify({"status": "success", "message": t("dashboard.fulfillment_completed"),
                        "data": result})
    except ValueError as error:
        return invalid_request_response(error)


def run_api():
    dashboard_logger.info(
        "Dashboard API is starting on %s:%s.",
        deployment_settings.dashboard_host,
        deployment_settings.dashboard_port,
    )
    from waitress import serve
    serve(
        app,
        host=deployment_settings.dashboard_host,
        port=deployment_settings.dashboard_port,
        threads=4,
    )


def start_dashboard_thread(bot=None):
    """Run Flask beside the bot without blocking the Discord event loop."""
    global _dashboard_bot
    _dashboard_bot = bot
    threading.Thread(target=run_api, daemon=True).start()


CONTROL_ACTION_RETENTION_DAYS = 30
# Pruning shares the worker's idle path rather than owning a timer of its own.
CONTROL_ACTION_PRUNE_INTERVAL = 3600


async def execute_member_erasure(bot, action) -> str | None:
    """Carry out an operator-requested erasure. Returns an error code or None.

    The dashboard process cannot revoke a Discord grant, so the route queues the
    request and the bot performs it here: grants first, then the database, so no
    premium role or rented asset outlives the record that would have expired it.
    """
    from cogs.privacy import revoke_all_entitlements

    # Re-derive authority here rather than trust the queued row: the action may
    # have been enqueued before ADMIN_DISCORD_ID last changed.
    if not ADMIN_ID or str(action["actor_id"]) != ADMIN_ID:
        return "permission_denied"
    subject_id = action["payload"].get("user_id")
    try:
        subject_id = int(subject_id)
    except (TypeError, ValueError):
        return "invalid_subject"
    if subject_id <= 0:
        return "invalid_subject"
    try:
        revoked = await revoke_all_entitlements(bot, subject_id)
        receipt = await database.run_write(
            database.anonymize_user, subject_id, action["actor_id"],
            action["guild_id"], "operator_request",
        )
    except database.ValidationError:
        return "invalid_subject"
    except database.DatabaseOperationError:
        dashboard_logger.exception("Operator-requested erasure failed.")
        return "internal_error"
    dashboard_logger.info(
        "Operator-requested erasure complete (tombstone=%s, revoked=%s).",
        receipt["tombstone_id"], revoked,
    )
    return None


async def control_action_worker(bot):
    """Execute queued Discord publishes after live permission and feature checks."""
    next_prune = 0.0
    while not bot.is_closed():
        if time.monotonic() >= next_prune:
            next_prune = time.monotonic() + CONTROL_ACTION_PRUNE_INTERVAL
            try:
                removed = await database.run_write(
                    database.prune_control_actions, CONTROL_ACTION_RETENTION_DAYS
                )
                if removed:
                    dashboard_logger.info(
                        "Pruned settled control actions (removed=%s)", removed
                    )
            except database.DatabaseOperationError:
                dashboard_logger.exception("Control action pruning failed.")

        action = await database.run_write(database.claim_control_action)
        if action is None:
            await asyncio.sleep(2)
            continue
        error_code = None
        try:
            if action["action_type"] == "erase_member":
                # Erasure needs neither a channel nor a document, and its authority
                # comes from ADMIN_DISCORD_ID rather than guild permissions, so it
                # is handled before the publish preamble below.
                error_code = await execute_member_erasure(bot, action)
                await database.run_write(
                    database.finish_control_action, action["action_id"],
                    error_code is None, error_code,
                )
                continue
            guild = bot.get_guild(action["guild_id"])
            actor = guild.get_member(action["actor_id"]) if guild else None
            channel = guild.get_channel(action["payload"].get("channel_id")) if guild else None
            if not guild or not actor or not actor.guild_permissions.manage_guild:
                error_code = "permission_denied"
            elif not isinstance(channel, discord.TextChannel):
                error_code = "channel_unavailable"
            elif not channel.permissions_for(guild.me).send_messages:
                error_code = "bot_permission_denied"
            else:
                documents = {
                    item["document_id"]: item
                    for item in await database.run_read(
                        database.list_dashboard_documents, guild.id
                    )
                }
                document = documents.get(action["payload"].get("document_id"))
                if not document:
                    error_code = "document_unavailable"
                elif action["action_type"] == "send_embed" and is_enabled(guild.id, "general"):
                    content = document["content"]
                    embed = discord.Embed(
                        title=content.get("title"), description=content.get("description"),
                        color=int(content.get("color", 0xF5B041)),
                    )
                    for field in content.get("fields", []):
                        embed.add_field(name=str(field.get("name", ""))[:256],
                                        value=str(field.get("value", ""))[:1024],
                                        inline=bool(field.get("inline", False)))
                    if content.get("footer"):
                        embed.set_footer(text=str(content["footer"])[:2048])
                    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                elif action["action_type"] == "publish_rules" and is_enabled(guild.id, "onboarding"):
                    for index, section in enumerate(document["content"].get("sections", [])):
                        # Discord rate limiting can stretch a multi-section
                        # publish well past the lease, so renew as we go rather
                        # than let another worker reclaim and repost it.
                        if index and index % 5 == 0:
                            await database.run_write(
                                database.renew_control_action_lease, action["action_id"]
                            )
                        embed = discord.Embed(
                            title=str(section.get("title", ""))[:256],
                            description=str(section.get("description", ""))[:4096],
                            color=0xF5B041,
                        )
                        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                elif action["action_type"] == "publish_panel":
                    content = document["content"]
                    panel_type = content.get("panel_type")
                    view = None
                    if panel_type == "tickets" and is_enabled(guild.id, "tickets"):
                        from cogs.tickets import TicketLauncher
                        view = TicketLauncher()
                    elif panel_type == "airlock" and is_enabled(guild.id, "onboarding"):
                        from cogs.admin import EnterServerView
                        view = EnterServerView()
                    elif panel_type == "role_menu" and is_enabled(guild.id, "role_menus"):
                        from cogs.roleselect import GameRoleView, NewsRoleView, ThemesRoleView
                        variant = content.get("options", {}).get("variant", "games")
                        view_class = {"games": GameRoleView, "news": NewsRoleView,
                                      "themes": ThemesRoleView}.get(variant)
                        if view_class:
                            view = view_class()
                    if view is None:
                        error_code = "feature_disabled_or_invalid_panel"
                    else:
                        embed = discord.Embed(
                            title=str(content.get("title", ""))[:256],
                            description=str(content.get("description", ""))[:4096],
                            color=0xF5B041,
                        )
                        await channel.send(embed=embed, view=view,
                                           allowed_mentions=discord.AllowedMentions.none())
                else:
                    error_code = "feature_disabled_or_unsupported"
        except discord.HTTPException:
            dashboard_logger.exception("Dashboard control action failed (action_id=%s)", action["action_id"])
            error_code = "discord_http_error"
        except Exception:
            dashboard_logger.exception("Dashboard control action crashed (action_id=%s)", action["action_id"])
            error_code = "internal_error"
        await database.run_write(
            database.finish_control_action, action["action_id"], error_code is None, error_code
        )


if __name__ == "__main__":
    run_api()

"""HTTP client for the Fantasy Premier League API.

FPL publishes no API contract, so every response is schema-checked before use.
Drift raises SchemaDrift rather than letting the pipeline project from fields
it has misread.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import requests

log = logging.getLogger(__name__)

BASE = "https://fantasy.premierleague.com/api"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Fields the rest of the pipeline reads. Absence means the model would be
# silently computing on defaults, so we fail instead.
REQUIRED_BOOTSTRAP_KEYS = {
    "elements", "events", "teams", "element_types", "game_settings", "game_config",
}
REQUIRED_ELEMENT_FIELDS = {
    "id", "web_name", "team", "element_type", "now_cost", "status", "minutes",
    "total_points", "form", "selected_by_percent", "ep_next", "starts",
    "expected_goals_per_90", "expected_assists_per_90",
    "expected_goals_conceded_per_90", "defensive_contribution",
    "defensive_contribution_per_90", "chance_of_playing_next_round",
    "penalties_order", "bps", "news", "news_added", "can_select",
}
REQUIRED_FIXTURE_FIELDS = {
    "id", "event", "team_h", "team_a", "team_h_difficulty", "team_a_difficulty",
    "kickoff_time", "finished",
}


class SchemaDrift(RuntimeError):
    """The API returned a payload missing fields the pipeline depends on."""


class AuthExpired(RuntimeError):
    """The stored session cookie is no longer valid."""


class Client:
    """Polite, retrying FPL API client.

    Authentication is optional: every read the projection model needs is public.
    A token is only required for `my_team` and the write path.

    FPL authenticates with an OAuth bearer token in the `x-api-authorization`
    header, issued by PingOne at account.premierleague.com. Session cookies are
    not credentials — sending them alone returns 403 "Authentication credentials
    were not provided". Scripted password login is gone too, so the token has to
    come from a real browser session for now.

    Access tokens live 8 hours, which is short enough that a hand-pasted one
    cannot survive to a scheduled deadline run. Minting tokens from a refresh
    token is the next piece of work; until then `FPL_ACCESS_TOKEN` is a
    short-lived convenience for interactive use.

    FPL_COOKIE is still honoured because Cloudflare and DataDome cookies may be
    needed alongside the token from some IPs, but on its own it authenticates
    nothing.
    """

    def __init__(self, cookie: str | None = None, access_token: str | None = None,
                 min_interval: float = 1.0, timeout: float = 30.0,
                 max_retries: int = 4):
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self._lock = threading.Lock()
        self._last_request = 0.0

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept": "application/json"})
        cookie = cookie if cookie is not None else os.environ.get("FPL_COOKIE", "")
        if cookie:
            # Bot-protection cookies only. These do not authenticate on their own.
            self.session.headers["Cookie"] = cookie

        token = (access_token if access_token is not None
                 else os.environ.get("FPL_ACCESS_TOKEN", "")).strip()
        if token:
            # Strip a pasted "Bearer " prefix so both forms work.
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
            self.session.headers["X-Api-Authorization"] = f"Bearer {token}"

        self.has_token = bool(token)
        self.has_cookie = bool(cookie)
        # Kept for callers written against the old cookie-only assumption.
        self.authenticated_reads_possible = self.has_token

    # ---------- transport ----------

    def _throttle(self) -> None:
        """Space requests out so we never hammer an API we don't own."""
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def get(self, path: str) -> dict | list:
        url = f"{BASE}/{path.lstrip('/')}"
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            self._throttle()
            try:
                r = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("%s failed (%s), retry %d", path, exc, attempt + 1)
                time.sleep(2 ** attempt)
                continue

            if r.status_code == 200:
                return r.json()
            if r.status_code in (401, 403):
                # 401 means FPL read a token and rejected it; 403 means it saw no
                # credentials at all. The distinction is the whole diagnosis.
                hint = ("token expired or malformed — access tokens last 8 hours"
                        if self.has_token else
                        "no FPL_ACCESS_TOKEN set — cookies alone do not authenticate")
                raise AuthExpired(f"{path} returned {r.status_code}: {hint}")
            if r.status_code == 429 or r.status_code >= 500:
                delay = 2 ** attempt
                log.warning("%s returned %d, backing off %ds", path, r.status_code, delay)
                last_error = RuntimeError(f"{path} -> HTTP {r.status_code}")
                time.sleep(delay)
                continue
            raise RuntimeError(f"{path} -> HTTP {r.status_code}: {r.text[:200]}")

        raise RuntimeError(f"{path} failed after {self.max_retries} attempts") from last_error

    # ---------- public reads ----------

    def bootstrap_static(self) -> dict:
        data = self.get("bootstrap-static/")
        missing = REQUIRED_BOOTSTRAP_KEYS - set(data)
        if missing:
            raise SchemaDrift(f"bootstrap-static missing top-level keys: {sorted(missing)}")
        if not data["elements"]:
            raise SchemaDrift("bootstrap-static returned zero players")
        missing_fields = REQUIRED_ELEMENT_FIELDS - set(data["elements"][0])
        if missing_fields:
            raise SchemaDrift(f"player records missing fields: {sorted(missing_fields)}")
        return data

    def fixtures(self) -> list:
        data = self.get("fixtures/")
        if not isinstance(data, list) or not data:
            raise SchemaDrift("fixtures returned no rows")
        missing = REQUIRED_FIXTURE_FIELDS - set(data[0])
        if missing:
            raise SchemaDrift(f"fixture records missing fields: {sorted(missing)}")
        return data

    def element_summary(self, element_id: int) -> dict:
        return self.get(f"element-summary/{element_id}/")

    def event_live(self, gw: int) -> dict:
        return self.get(f"event/{gw}/live/")

    def entry(self, entry_id: int) -> dict:
        return self.get(f"entry/{entry_id}/")

    def entry_history(self, entry_id: int) -> dict:
        return self.get(f"entry/{entry_id}/history/")

    # ---------- authenticated reads ----------

    def me(self) -> dict:
        return self.get("me/")

    def is_authenticated(self) -> bool:
        """Cheap auth health check. Every scheduled run starts here.

        `me/` answers 200 with `{"player": null}` when unauthenticated rather than
        erroring, so a wrong credential fails clearly instead of silently.
        """
        try:
            return bool(self.me().get("player"))
        except (AuthExpired, RuntimeError) as exc:
            log.warning("auth check failed: %s", exc)
            return False

    def my_team(self, entry_id: int) -> dict:
        """Current squad, bank, free transfers, chips. Needs a live bearer token."""
        if not self.has_token:
            raise AuthExpired(
                "my-team requires FPL_ACCESS_TOKEN; session cookies are not "
                "credentials for this API"
            )
        return self.get(f"my-team/{entry_id}/")

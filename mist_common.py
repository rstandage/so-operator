#!/usr/bin/env python3
"""
mist_common.py - shared helpers for the Mist "Switch Port Operator" port toolkit.

Shared by:
  * enable_port_operator.py   (unlock no_local_overwrite on selected port profiles)
  * disable_port_operator.py  (counter-rollback from stored backups)

Every endpoint used by this toolkit is listed below and was verified against the
official Juniper Mist OpenAPI 3.1 specification ("Mist API 2607.1.1"):
  GET  /api/v1/self                                          - token self lookup
  GET  /api/v1/orgs/{org_id}                                 - org details
  GET  /api/v1/orgs/{org_id}/sites                           - list sites in an org
  GET  /api/v1/sites/{site_id}                               - site details
  GET  /api/v1/sites/{site_id}/devices                       - list devices (paginated)
  GET  /api/v1/sites/{site_id}/devices/{device_id}           - read device config
  PUT  /api/v1/sites/{site_id}/devices/{device_id}           - update device config
  GET  /api/v1/orgs/{org_id}/networktemplates                - list network templates
                                                               (port_usages = port profiles)
  PUT  /api/v1/sites/{site_id}/devices/{device_id}/local_port_config
       - replace local switch-port overrides (used by Switch Port Operators)

Authentication (verified against the spec's securitySchemes):
  Authorization: Token <api-token>
  Org API tokens are preferred for automation; the token must be scoped to the
  single organisation being changed.

Design rules for this toolkit:
  * Python 3.11+ standard library only - no third-party dependencies.
  * One organisation per run, enforced via the token's own privileges.
  * Every device is backed up before any change is made.
  * The token value is never printed to the terminal or written to logs.

MIT License - see the LICENSE file. Provided as-is with no support.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

__version__ = "1.0.1"

DEFAULT_BASE_URL = "https://api.mist.com"
DEFAULT_TIMEOUT = 30.0          # seconds per HTTP attempt
DEFAULT_MAX_RETRIES = 5         # retries for 429 / 5xx / network errors
DEFAULT_DELAY = 0.2             # polite pacing between API calls (seconds)
RETRY_BASE_SLEEP = 1.5          # exponential backoff base for retries

USER_AGENT = f"mist-port-operator-toolkit/{__version__} (MIT; unsupported)"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class MistApiError(Exception):
    """A Mist API call failed (after retries, where retries apply)."""

    def __init__(self, message: str, status: int | None = None,
                 endpoint: str = "", detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint
        self.detail = detail


class MistAuthError(MistApiError):
    """Authentication / authorisation failure (HTTP 401 or 403)."""


# --------------------------------------------------------------------------- #
# Terminal + file logging
# --------------------------------------------------------------------------- #

_LOG_FILE_HANDLE = None
_LOG_FILE_PATH: Path | None = None


def init_file_logging(script_name: str, log_dir: str = "logs") -> Path:
    """Start mirroring all log()/dbg()/warn() output into logs/<name>_<ts>.log."""
    global _LOG_FILE_HANDLE, _LOG_FILE_PATH
    if _LOG_FILE_PATH is not None:
        return _LOG_FILE_PATH
    root = Path(log_dir)
    root.mkdir(parents=True, exist_ok=True)
    _LOG_FILE_PATH = root / f"{script_name}_{utc_ts()}.log"
    _LOG_FILE_HANDLE = open(_LOG_FILE_PATH, "a", encoding="utf-8")
    return _LOG_FILE_PATH


def log(message: Any = "", end: str = "\n") -> None:
    """Print a progress line to the terminal and to the log file."""
    line = str(message)
    print(line, end=end, flush=True)
    if _LOG_FILE_HANDLE:
        _LOG_FILE_HANDLE.write(line + end)
        _LOG_FILE_HANDLE.flush()


def dbg(message: str, enabled: bool = False) -> None:
    """Emit a debug line (stderr + log file) when debugging is enabled."""
    if not enabled:
        return
    line = f"[DEBUG] {message}"
    print(line, file=sys.stderr, flush=True)
    if _LOG_FILE_HANDLE:
        _LOG_FILE_HANDLE.write(line + "\n")
        _LOG_FILE_HANDLE.flush()


def warn(message: str) -> None:
    """Emit a warning line."""
    log(f"[WARN]  {message}")


def die(message: str, code: int = 1) -> NoReturn:
    """Print an error and exit. Used for fatal, user-actionable problems."""
    log(f"[ERROR] {message}")
    if _LOG_FILE_HANDLE:
        _LOG_FILE_HANDLE.flush()
    sys.exit(code)


def log_exception(prefix: str = "Unexpected internal failure") -> None:
    """Log a full traceback (terminal + log file) for unexpected errors.

    Used by the scripts' top-level handlers so that a crash never disappears
    without a trace in the run's log file.
    """
    log("")
    log(f"[ERROR] {prefix} - traceback follows:")
    for tb_line in (traceback.format_exc().splitlines() or ["<no traceback>"]):
        log(tb_line)


# --------------------------------------------------------------------------- #
# Pacing (rate-limit friendliness)
# --------------------------------------------------------------------------- #

_PACING_DELAY = DEFAULT_DELAY


def set_pacing(delay_seconds: float) -> None:
    """Set the minimum delay between consecutive API calls."""
    global _PACING_DELAY
    _PACING_DELAY = max(0.0, float(delay_seconds))


def pace() -> None:
    """Sleep for the configured pacing delay."""
    if _PACING_DELAY > 0:
        time.sleep(_PACING_DELAY)


# --------------------------------------------------------------------------- #
# Configuration loading (.env, token, base URL) - stdlib only
# --------------------------------------------------------------------------- #

def load_env_file(explicit_path: str | None = None) -> dict[str, str]:
    """Load KEY=VALUE pairs from a .env file without any third-party library.

    Search order: explicit path -> ./.env -> .env next to this source file.
    Handles comments (#), blank lines, an optional 'export ' prefix and
    single- or double-quoted values. A missing file is not an error.
    """
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).resolve().parent / ".env")
    for path in candidates:
        if path.is_file():
            values: dict[str, str] = {}
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    values[key] = value
            return values
    return {}


def resolve_token(args_token: str | None, env: dict[str, str]) -> str:
    """Resolve the API token from --token, the environment, or .env (that order).

    The token value itself is never logged or printed.
    """
    token = (
        (args_token or "").strip()
        or (os.environ.get("MIST_API_TOKEN") or "").strip()
        or (env.get("MIST_API_TOKEN") or "").strip()
    )
    if not token:
        die(
            "No API token provided. Supply it via --token, the MIST_API_TOKEN "
            "environment variable, or MIST_API_TOKEN=... inside a .env file in "
            "the project folder. Generate a dedicated Org API token for this "
            "purpose - it must be scoped to the single organisation being changed."
        )
    if len(token) < 16 or any(ch.isspace() for ch in token):
        die("The provided API token does not look valid "
            "(too short, or contains whitespace).")
    return token


def resolve_base_url(args_base_url: str | None, env: dict[str, str]) -> str:
    """Resolve the API base URL (--base-url, env, .env, then the default).

    Regional clouds such as https://api.eu.mist.com are supported - just pass
    the region's base URL.
    """
    raw = (
        (args_base_url or "").strip()
        or (os.environ.get("MIST_BASE_URL") or "").strip()
        or (env.get("MIST_BASE_URL") or "").strip()
        or DEFAULT_BASE_URL
    )
    if not raw.startswith(("http://", "https://")):
        die(f"Base URL must start with http:// or https:// - got '{raw}'")
    return raw.rstrip("/")


def mask_token(token: str) -> str:
    """Return a safe, non-reversible description of a token for logging."""
    if len(token) <= 8:
        return "****"
    return f"{token[:4]}...{token[-4:]} (length {len(token)})"


# --------------------------------------------------------------------------- #
# HTTP core with retry/backoff and 429 handling
# --------------------------------------------------------------------------- #

MAX_HTTP_DEBUG_BODY = 4000      # max body characters echoed in debug output
RETRY_BASE_SLEEP = 1.5          # exponential backoff base for retries

_LAST_RATE_LIMIT_INFO: dict[str, int | None] = {"limit": None, "remaining": None}


def _rate_limit_snapshot() -> str:
    """Human-readable summary of the last observed rate-limit headers."""
    limit = _LAST_RATE_LIMIT_INFO.get("limit")
    remaining = _LAST_RATE_LIMIT_INFO["remaining"]
    if limit is None:
        return "rate-limit headers not seen"
    return f"approx {remaining}/{limit} requests remaining"


def _parse_retry_after(value: str | None) -> float:
    """Parse a Retry-After header into seconds (seconds form or HTTP-date)."""
    if not value:
        return 0.0
    value = value.strip()
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return 0.0


class MistClient:
    """Minimal, dependency-free Mist API client with retries and pacing."""

    def __init__(self, token: str, base_url: str, *,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 debug: bool = False) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.debug = debug
        self.request_count = 0
        self._last_call_time: float = 0.0

    # -- low level ---------------------------------------------------------- #

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Token {self.token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _request(self, method: str, endpoint: str, *,
                 params: dict | None = None,
                 json_body: dict | None = None,
                 expected: tuple = (200,)) -> Any:
        """Perform one API call with retry/backoff. endpoint starts with /api/."""
        if params:
            endpoint = f"{endpoint}?{urllib.parse.urlencode(params)}"
        url = f"{self.base_url}{endpoint}"
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")

        attempt = 0
        while True:
            attempt += 1
            if self._last_call_time:
                elapsed = time.monotonic() - self._last_call_time
                if elapsed < _PACING_DELAY:
                    time.sleep(_PACING_DELAY - elapsed)

            req = urllib.request.Request(url, data=data, method=method)
            for key, value in self._headers(
                    "application/json" if data else None).items():
                req.add_header(key, value)
            if self.debug:
                body_preview = "(none)"
                if data:
                    body_preview = data.decode("utf-8", "replace")
                    if len(body_preview) > MAX_HTTP_DEBUG_BODY:
                        body_preview = body_preview[:MAX_HTTP_DEBUG_BODY] + " ...[trunc]"
                dbg(f"HTTP {method} {url} | attempt {attempt} | body: {body_preview}", True)

            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    self._last_call_time = time.monotonic()
                    self.request_count += 1
                    limit_hdr = resp.headers.get("X-RateLimit-Limit")
                    rem_hdr = resp.headers.get("X-RateLimit-Remaining")
                    if limit_hdr and limit_hdr.isdigit():
                        _LAST_RATE_LIMIT_INFO["limit"] = int(limit_hdr)
                    if rem_hdr and rem_hdr.isdigit():
                        _LAST_RATE_LIMIT_INFO["remaining"] = int(rem_hdr)
                    if resp.status in expected:
                        if not raw:
                            return None
                        try:
                            return json.loads(raw.decode("utf-8"))
                        except json.JSONDecodeError as exc:
                            raise MistApiError(
                                f"Response was not valid JSON: {exc}",
                                status=resp.status, endpoint=endpoint) from exc
                    raise MistApiError(
                        f"Unexpected status {resp.status} (expected {expected})",
                        status=resp.status, endpoint=endpoint)

            except urllib.error.HTTPError as exc:
                self._last_call_time = time.monotonic()
                body = exc.read().decode("utf-8", "replace")
                status = exc.code
                if self.debug:
                    dbg(f"HTTP {status} on {endpoint} | "
                        f"body: {body[:MAX_HTTP_DEBUG_BODY]}", True)
                if status in (401, 403):
                    raise MistAuthError(
                        f"Authentication/authorisation failed (HTTP {status}) "
                        f"on {endpoint}. Check the token value and that it is "
                        "scoped to this organisation.",
                        status=status, endpoint=endpoint, detail=body) from exc

                if status == 429:
                    if attempt > self.max_retries:
                        raise MistApiError(
                            f"Rate limited (HTTP 429) on {endpoint} - gave up "
                            f"after {self.max_retries} retries.",
                            status=status, endpoint=endpoint, detail=body) from exc
                    wait = max(_parse_retry_after(exc.headers.get("Retry-After")),
                               RETRY_BASE_SLEEP * (2 ** (attempt - 1))
                               + random.uniform(0, 0.5))
                    warn(f"HTTP 429 rate limited on {endpoint}; sleeping "
                         f"{wait:.1f}s (attempt {attempt}/{self.max_retries})")
                    time.sleep(wait)
                    continue
                if status >= 500:
                    if attempt > self.max_retries:
                        raise MistApiError(
                            f"Server error HTTP {status} on {endpoint} after "
                            f"{self.max_retries} retries.",
                            status=status, endpoint=endpoint, detail=body) from exc
                    wait = RETRY_BASE_SLEEP * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    warn(f"HTTP {status} server error on {endpoint}; retrying "
                         f"in {wait:.1f}s (attempt {attempt}/{self.max_retries})")
                    time.sleep(wait)
                    continue
                raise MistApiError(f"HTTP {status} on {endpoint}", status=status,
                                   endpoint=endpoint, detail=body) from exc

            except urllib.error.URLError as exc:
                # covers DNS failures, refused connections, TLS problems
                self._last_call_time = time.monotonic()
                if attempt > self.max_retries:
                    raise MistApiError(
                        f"Network error calling {endpoint}: {exc.reason}",
                        endpoint=endpoint) from exc
                wait = RETRY_BASE_SLEEP * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                warn(f"Network error ({exc.reason}); retrying in {wait:.1f}s "
                     f"(attempt {attempt}/{self.max_retries})")
                time.sleep(wait)
                continue
            except TimeoutError as exc:
                self._last_call_time = time.monotonic()
                if attempt > self.max_retries:
                    raise MistApiError(
                        f"Timed out calling {endpoint} after "
                        f"{self.max_retries} retries.", endpoint=endpoint) from exc
                wait = RETRY_BASE_SLEEP * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                warn(f"Timeout; retrying in {wait:.1f}s "
                     f"(attempt {attempt}/{self.max_retries})")
                time.sleep(wait)
                continue

    # -- typed wrappers ------------------------------------------------------ #

    def get(self, endpoint: str, *, params: dict | None = None) -> Any:
        return self._request("GET", endpoint, params=params)

    def put(self, endpoint: str, body: dict) -> Any:
        return self._request("PUT", endpoint, json_body=body)

    # -- pagination ----------------------------------------------------------- #

    def get_all_pages(self, endpoint: str, *, params: dict | None = None,
                      label: str = "objects") -> list[dict]:
        """GET a paginated collection (limit/page) and return every item.

        All list endpoints used by this toolkit (sites, devices, network
        templates) paginate with limit/page and default limit=100.
        """
        items: list[dict] = []
        page = 1
        limit = 100
        while True:
            query = dict(params or {})
            query["limit"] = limit
            query["page"] = page
            batch = self.get(endpoint, params=query)
            if not isinstance(batch, list):
                raise MistApiError(
                    f"Expected a JSON array from {endpoint} but got "
                    f"{type(batch).__name__}", endpoint=endpoint)
            if not batch:
                break
            items.extend(batch)
            if len(batch) < limit:
                break
            page += 1
        dbg(f"paginated GET {endpoint} -> {len(items)} {label}", self.debug)
        return items


# --------------------------------------------------------------------------- #
# Auth, org/scope confirmation and site selection
# --------------------------------------------------------------------------- #

def token_self_lookup(client: MistClient, token: str, debug: bool) -> dict:
    """GET /api/v1/self - confirm the token works and return the self object."""
    try:
        me = client.get("/api/v1/self")
    except MistAuthError as exc:
        die(f"Token rejected by /api/v1/self: {exc}")
    if not isinstance(me, dict):
        die("/api/v1/self did not return an object - token or cloud mismatch?")
    dbg(f"/api/v1/self keys: {sorted(me.keys())}", debug)
    log(f"Token OK ({mask_token(token)}). "
        f"Authenticated as: {me.get('email') or me.get('name') or 'unknown'}")
    return me


def _privilege_sort_key(priv: dict) -> tuple:
    # Org-scope first, then site-scope, then unknown
    scope = str(priv.get("scope") or "").lower()
    return {"org": 0, "site": 1}.get(scope, 2)


def confirm_org_scope(client: MistClient, me: dict, env: dict,
                      debug: bool) -> dict:
    """Determine the single org this token can manage and confirm it.

    Resolution order:
      1. privileges[] on /api/v1/self (org/scope/role/org_id per the spec)
      2. MIST_ORG_ID in the environment or .env

    Returns {'org': org_dict, 'env_org_id': str|None}.
    """
    env_org_id = (os.environ.get("MIST_ORG_ID") or env.get("MIST_ORG_ID") or "").strip()
    org_id: str | None = None
    privileges: list[dict] = me.get("privileges") or []

    org_privs = sorted(
        (p for p in privileges if isinstance(p, dict) and p.get("org_id")),
        key=_privilege_sort_key)
    if org_privs:
        chosen = org_privs[0]
        org_id = str(chosen.get("org_id") or "").strip() or None
        if debug:
            for p in org_privs:
                dbg(f"privilege: scope={p.get('scope')} role={p.get('role')} "
                    f"org_id={p.get('org_id')} site_id={p.get('site_id')}", True)

    if not org_id and env_org_id:
        org_id = env_org_id
        dbg("org id taken from MIST_ORG_ID (env/.env)", debug)

    if not org_id:
        die("Could not determine an organisation from the token privileges "
            "or MIST_ORG_ID. Org-scoped API tokens always carry an org_id "
            "privilege; check the token you generated.")

    try:
        org = client.get(f"/api/v1/orgs/{org_id}")
    except MistApiError as exc:
        die(f"Cannot read organisation {org_id}: {exc}")
    if not isinstance(org, dict) or not org.get("id"):
        die(f"Organisation {org_id} could not be read (bad response).")

    log(f"Organisation: {org.get('name', '?')} (id {org.get('id')})")

    if env_org_id and org_id != env_org_id:
        die(f"Safety stop: MIST_ORG_ID is {env_org_id} but the token resolves "
            f"to org {org_id}. Fix the mismatch before proceeding.")

    return {"org": org, "env_org_id": env_org_id or None}


def list_org_sites(client: MistClient, org_id: str) -> list[dict]:
    """GET /api/v1/orgs/{org_id}/sites (paginated)."""
    return client.get_all_pages(f"/api/v1/orgs/{org_id}/sites", label="sites")


def select_site(client: MistClient, org: dict, env: dict,
                args_site: str | None, assume_yes: bool) -> tuple[str, str]:
    """Pick the target site (or all sites) interactively or via --site/--all-sites.

    Returns (site_id, site_name); site_name == ALL_SITES_LABEL for all sites.
    """
    org_id = org["id"]
    env_site = (os.environ.get("MIST_SITE_ID") or env.get("MIST_SITE_ID") or "").strip()

    if args_site:
        sites = list_org_sites(client, org_id)
        wanted = args_site.strip()
        for site in sites:
            if wanted.lower() in (str(site.get("id", "")).lower(),
                                  str(site.get("name", "")).lower(),
                                  str(site.get("sitecode", "")).lower()):
                log(f"Site selected via --site: {site.get('name')} (id {site.get('id')})")
                return str(site["id"]), str(site.get("name") or site["id"])
        die(f"--site '{wanted}' did not match any site in this organisation "
            f"({len(sites)} site(s) visible).")

    if env_site and not args_site:
        sites = list_org_sites(client, org_id)
        for site in sites:
            if str(site.get("id")) == env_site:
                log(f"Site selected via MIST_SITE_ID: {site.get('name')} "
                    f"(id {site.get('id')})")
                return str(site["id"]), str(site.get("name") or site["id"])
        die(f"MIST_SITE_ID={env_site} does not match any site in this organisation.")

    sites = list_org_sites(client, org_id)
    if not sites:
        die("No sites are visible with this token - nothing to do.")

    if assume_yes:
        # Non-interactive default: first site, clearly announced
        site = sites[0]
        log(f"--yes given: defaulting to first site: {site.get('name')} "
            f"(id {site.get('id')})")
        return str(site["id"]), str(site.get("name") or site["id"])

    log("")
    log("Sites available:")
    for idx, site in enumerate(sites, start=1):
        log(f"  {idx:>2}) {site.get('name', '?')} "
            f"[{site.get('sitecode') or 'no sitecode'}] id={site.get('id')}")
    log(f"   A) ALL sites in this organisation ({len(sites)} site(s))")
    while True:
        try:
            answer = input("Select site number (or 'A' for all sites): ").strip()
        except EOFError:
            die("No interactive input available; use --site or --all-sites.")
        if answer.upper() == "A":
            return ALL_SITES_LABEL, ALL_SITES_LABEL
        if answer.isdigit() and 1 <= int(answer) <= len(sites):
            site = sites[int(answer) - 1]
            return str(site["id"]), str(site.get("name") or site["id"])
        log("Please enter a valid number or 'A'.")


# --------------------------------------------------------------------------- #
# Device / port model helpers (verified against device_switch / junos_port_config)
# --------------------------------------------------------------------------- #

ALL_SITES_LABEL = "ALL_SITES"


def fetch_switches(client: MistClient, site_id: str) -> list[dict]:
    """GET /api/v1/sites/{site_id}/devices?type=switch (paginated).

    NOTE: the device-list 'type' query parameter defaults to 'ap' in the Mist
    API, so type=switch must always be passed explicitly.
    """
    devices = client.get_all_pages(
        f"/api/v1/sites/{site_id}/devices",
        params={"type": "switch"}, label="switches")
    return [d for d in devices if str(d.get("type", "")).lower() == "switch"]


def get_device(client: MistClient, site_id: str, device_id: str) -> dict:
    """GET /api/v1/sites/{site_id}/devices/{device_id}."""
    return client.get(f"/api/v1/sites/{site_id}/devices/{device_id}")


def put_device(client: MistClient, site_id: str, device_id: str,
               body: dict) -> dict:
    """PUT /api/v1/sites/{site_id}/devices/{device_id}."""
    return client.put(f"/api/v1/sites/{site_id}/devices/{device_id}", body)


def get_port_config(device: dict) -> dict[str, dict]:
    """Return the device's port_config map ({port: junos_port_config})."""
    cfg = device.get("port_config")
    return cfg if isinstance(cfg, dict) else {}


def get_device_port_usages(device: dict) -> dict[str, dict]:
    """Return device-level port_usages (reusable profiles on this switch)."""
    pu = device.get("port_usages")
    return pu if isinstance(pu, dict) else {}


def list_port_profiles(client: MistClient, org_id: str,
                       devices: list[dict]) -> dict[str, dict]:
    """Collect every switch port profile visible to this org.

    Sources (per the OpenAPI spec):
      * port_usages inside each org network template
        (GET /api/v1/orgs/{org_id}/networktemplates)
      * device-level port_usages on each switch

    Returns {profile_name: {"name":..., "sources": [...], "type":...}}.
    """
    profiles: dict[str, dict] = {}

    try:
        templates = client.get_all_pages(
            f"/api/v1/orgs/{org_id}/networktemplates", label="templates")
    except MistApiError as exc:
        warn(f"Could not list org network templates ({exc}); "
             "continuing with device-level profiles only.")
        templates = []

    for template in templates:
        for name, usage in (template.get("port_usages") or {}).items():
            entry = profiles.setdefault(
                str(name), {"name": str(name), "sources": []})
            entry["sources"].append(f"template:{template.get('name', '?')}")

    for device in devices:
        for name in get_device_port_usages(device):
            entry = profiles.setdefault(
                str(name), {"name": str(name), "sources": []})
            entry["sources"].append(f"device:{device.get('name') or device.get('id')}")

    return profiles


def _port_usage_of(port_cfg: dict) -> str | None:
    """The port profile name a port is set to ('usage' in junos_port_config)."""
    usage = port_cfg.get("usage")
    if usage is None:
        return None
    return str(usage)


def _port_disabled(port_cfg: dict) -> bool:
    """True when the port is administratively disabled."""
    return bool(port_cfg.get("disabled", False))


def find_target_ports(device: dict, profile_names: set[str]) -> list[tuple[str, dict]]:
    """Ports on this device whose 'usage' matches one of the profile names.

    Returns [(port_name, port_config_dict), ...] sorted naturally by port.
    """
    targets: list[tuple[str, dict]] = []
    for port, cfg in get_port_config(device).items():
        if not isinstance(cfg, dict):
            continue
        if _port_usage_of(cfg) in profile_names:
            targets.append((str(port), cfg))
    return natural_sort_ports(targets)


def port_needs_change(port_cfg: dict) -> bool:
    """True when no_local_overwrite is not explicitly False (i.e. locked or unset)."""
    return port_cfg.get("no_local_overwrite", True) is not False


# --------------------------------------------------------------------------- #
# Backups, CSV summary, argparse helpers
# --------------------------------------------------------------------------- #

def new_backup_folder(base_dir: str, prefix: str = "backups") -> Path:
    """Create and return a timestamped backup folder: backups/<prefix>_<ts>."""
    root = Path(base_dir)
    root.mkdir(parents=True, exist_ok=True)
    folder = root / f"{prefix}_{utc_ts()}"
    folder.mkdir(parents=True, exist_ok=False)
    return folder


def backup_device(device: dict, folder: Path, site_name: str) -> Path:
    """Write one labelled per-device backup JSON into the folder.

    Filename: <site>_<devicename>_<last-6-of-id>.json (sanitised, safe names).
    """
    site_slug = sanitize_filename(site_name)
    name_slug = sanitize_filename(device.get("name") or device.get("id") or "device")
    short_id = str(device.get("id") or "")[-6:]
    path = folder / f"{site_slug}_{name_slug}_{short_id}.json"
    payload = {
        "backup_version": 1,
        "captured_at_utc": utc_ts(),
        "toolkit_version": __version__,
        "site_id": str(device.get("site_id") or ""),
        "site_name": site_name,
        "device_id": str(device.get("id")),
        "device_name": device.get("name"),
        "device_type": device.get("type"),
        "device_model": device.get("model"),
        "device_serial": device.get("serial"),
        "device": device,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    dbg(f"backup written: {path.name}", False)
    return path


def write_csv_summary(path: Path, rows: list[dict]) -> None:
    """Write the CSV run summary using the stdlib csv module."""
    path = Path(path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("no changes\n", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    log(f"CSV summary written: {path}")


def natural_sort_ports(port_items: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """Sort port tuples like ge-0/0/2 < ge-0/0/10 (natural, human order)."""
    def key(item: tuple[str, Any]) -> tuple:
        name = item[0]
        parts = re.split(r"(\d+)", name)
        return tuple((1, int(p)) if p.isdigit() else (0, p) for p in parts)
    return sorted(port_items, key=key)


def sanitize_filename(value: str) -> str:
    """Make a string safe for use in a filename (keep it readable)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._-")
    return cleaned or "unnamed"


def utc_ts() -> str:
    """Timestamp used in filenames and logs: YYYYmmdd-HHMMSSZ."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Register the argparse options shared by both scripts."""
    parser.add_argument("--token",
                        help="Mist API token (else $MIST_API_TOKEN, else .env)")
    parser.add_argument("--base-url",
                        help="API base URL for regional clouds, "
                             "e.g. https://api.eu.mist.com")
    parser.add_argument("--site",
                        help="target site by id, name or sitecode")
    parser.add_argument("--all-sites", action="store_true",
                        help="apply to every site visible to the token")
    parser.add_argument("--backup-dir", default="backups",
                        help="folder for timestamped backups (default: backups)")
    parser.add_argument("--csv",
                        help="path for the CSV summary "
                             "(default: reports/<script>_<ts>.csv)")
    parser.add_argument("--yes", action="store_true",
                        help="non-interactive: skip prompts, take defaults")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would change without writing anything")
    parser.add_argument("--debug", action="store_true",
                        help="verbose debug output (URLs, payloads, HTTP bodies)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="HTTP timeout in seconds (default 30)")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")


def make_client_from_args(args: argparse.Namespace, script_name: str,
                          ) -> tuple[MistClient, dict, dict]:
    """Wire everything up: load .env, resolve token/base URL, build the client.

    Also starts file logging. Returns (client, me, env).
    """
    env = load_env_file()
    token = resolve_token(getattr(args, "token", None), env)
    base_url = resolve_base_url(getattr(args, "base_url", None), env)
    log_file = init_file_logging(script_name)
    log(f"Log file: {log_file}")
    if args.dry_run:
        log("DRY-RUN mode - no changes will be written.")
    client = MistClient(token=token, base_url=base_url,
                        timeout=args.timeout, debug=args.debug)
    me = token_self_lookup(client, token, args.debug)
    return client, me, env


def build_common_parser(description: str) -> argparse.ArgumentParser:
    """Create a parser pre-loaded with the shared options."""
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(parser)
    return parser


# --------------------------------------------------------------------------- #
# End of mist_common.py
# --------------------------------------------------------------------------- #
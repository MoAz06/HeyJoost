"""Shared helpers: HTTP client, TTL cache, time handling and errors."""

import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__

AMSTERDAM = ZoneInfo("Europe/Amsterdam")
# Name, version and contact, as public APIs like Transitous ask.
USER_AGENT = f"heyjoost/{__version__} (+https://heyjoost.nl; help@heyjoost.nl)"
PLACE_ALIASES = {"denhaag": "sgravenhage", "denbosch": "shertogenbosch"}

# Request URLs contain what users asked for (addresses, places), so keep them out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

_client: httpx.AsyncClient | None = None
_cache: dict[tuple, tuple[float, Any]] = {}
_CACHE_MAX_ENTRIES = 10_000


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
    return _client


async def cached(key: tuple, ttl: float, fetch: Callable[[], Awaitable[Any]]) -> Any:
    """Return a cached value, or fetch and store it for `ttl` seconds.

    Every user shares the same upstream budget, so all source calls go through here.
    """
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = await fetch()
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        _cache.clear()
    _cache[key] = (now + ttl, value)
    return value


async def get_json(
    url: str, *, params: dict | list | None = None, headers: dict | None = None, source: str
) -> Any:
    return await _request("GET", url, source=source, params=params, headers=headers)


async def post_json(url: str, body: Any, *, source: str) -> Any:
    return await _request("POST", url, source=source, json=body)


async def post_form(url: str, form: dict, *, source: str) -> Any:
    return await _request("POST", url, source=source, data=form)


async def _request(method: str, url: str, *, source: str, **kwargs: Any) -> Any:
    try:
        response = await client().request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise ToolError(f"{source} is nu niet bereikbaar ({type(exc).__name__}). Probeer het zo nog eens.") from exc
    if response.status_code in (401, 403):
        raise ToolError(f"{source} weigert de aanvraag (HTTP {response.status_code}). Controleer de API-key.")
    if response.status_code == 429:
        raise ToolError(f"{source} krijgt te veel aanvragen. Probeer het over een minuut opnieuw.")
    if response.status_code >= 400:
        raise ToolError(f"{source} gaf een fout (HTTP {response.status_code}).")
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


def now_local() -> datetime:
    return datetime.now(AMSTERDAM)


def parse_time(value: str | None) -> datetime | None:
    """Parse ISO timestamps, including NS-style offsets like +0200."""
    if not value:
        return None
    value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value.strip())
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=AMSTERDAM)
    return parsed.astimezone(AMSTERDAM)


def fmt_time(value: datetime | None) -> str | None:
    return value.strftime("%H:%M") if value else None


def fmt_datetime(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M") if value else None


def normalize(text: str) -> str:
    """Lowercase and strip everything except letters and digits, for name matching."""
    text = text.lower().replace("'s-", "s").replace("ij", "y")
    return re.sub(r"[^a-z0-9]", "", text)


def place_key(name: str) -> str:
    """Normalized place name, with Den Haag and Den Bosch mapped to their official names."""
    key = normalize(name)
    return PLACE_ALIASES.get(key, key)


def parse_moment(datum: str | None, tijd: str | None) -> datetime:
    """A local moment from the tools' datum (JJJJ-MM-DD) and tijd (UU:MM); a missing part means today or now."""
    now = now_local()
    try:
        day = date.fromisoformat(datum.strip()) if datum else now.date()
        if tijd:
            hour, minute = map(int, re.fullmatch(r"(\d{1,2})[:.](\d{2})", tijd.strip()).groups())
            at = dt_time(hour, minute)
        else:
            at = dt_time(now.hour, now.minute)
    except (ValueError, AttributeError) as exc:
        raise ToolError("Gebruik datum als JJJJ-MM-DD en tijd als UU:MM, bijvoorbeeld 2026-10-12 en 08:30.") from exc
    return datetime.combine(day, at, tzinfo=AMSTERDAM)

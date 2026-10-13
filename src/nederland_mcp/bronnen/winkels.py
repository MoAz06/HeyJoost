"""Supermarkets, pharmacies and cash machines near a place, with their opening hours, from OpenStreetMap.

The places come from a file that scripts/osm_winkels.py makes about once a month (data/osm_winkels.json), so no
question goes to OpenStreetMap. Only the address or place goes to PDOK, as with the other tools, and a station
is found in the list of stations of NS, which the server keeps for a day. The data is
© OpenStreetMap contributors under the ODbL, and every answer says so.

Opening hours are in OpenStreetMap's own notation. This reads the common forms ("Mo-Sa 08:00-21:00; Su 12:00-18:00",
"24/7", with PH for holidays); anything else is passed on as written, without saying whether it is open.
"""

import json
import math
import re
from datetime import date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from ..common import AMSTERDAM, now_local
from . import ns
from . import plaats as plaatsen
from .ov import _address, _is_address

DATA = Path(__file__).parent.parent / "data" / "osm_winkels.json"
BRON = "© OpenStreetMap-bijdragers (ODbL), openstreetmap.org/copyright"
KINDS = {
    "supermarkt": "supermarkt", "supermarkten": "supermarkt", "super": "supermarkt", "boodschappen": "supermarkt",
    "apotheek": "apotheek", "apotheken": "apotheek", "medicijnen": "apotheek",
    "pinautomaat": "pinautomaat", "pinautomaten": "pinautomaat", "geldautomaat": "pinautomaat", "pin": "pinautomaat",
    "pinnen": "pinautomaat", "geldmaat": "pinautomaat", "atm": "pinautomaat", "contant": "pinautomaat",
}
MAX_KM = 5
MAX_ITEMS = 10
# Machines for bitcoin are tagged as cash machines too, but give no cash.
CRYPTO = ("bitcoin", "crypto", "coin", "btc")
# Some liquor stores, butchers and other shops are tagged as supermarkets too. A supermarket with a liquor corner or
# a butcher has super in its name.
NO_SUPERMARKET = re.compile(
    r"\b(slijterij|liquor\w*|drank(en)?handel|wijnhandel|gall & gall|mitra|slagerij|pindakaas\w*|kaas(winkel|handel)"
    r"|wereldwinkel)\b",
    re.IGNORECASE,
)
LET_OP = "Openingstijden uit OpenStreetMap, ingevuld door vrijwilligers. Ze kunnen afwijken, zeker op feestdagen."
PHARMACY = (
    "Buiten openingstijden is er een dienstapotheek. Welke dat is, hoor je bij je eigen apotheek (ook op hun "
    "antwoordapparaat) of bij de huisartsenpost. Bij spoed bel je 112."
)
DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
DAY_NAMES = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")
DAY = r"(?:Mo|Tu|We|Th|Fr|Sa|Su|PH)"
SELECTOR = re.compile(rf"^((?:{DAY}(?:-{DAY})?)(?:\s*,\s*{DAY}(?:-{DAY})?)*)\s+(.*)$")
SPAN = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# A rule for some dates only, like "Dec 24 off" or "Jul,Aug 08:00-21:00". It is left out, except in its own months:
# then the hours are not read at all, rather than read wrong. Easter falls in March or April.
DATED = re.compile(r"^(?:\d{4}\s+)?((?:" + "|".join(MONTHS) + r")\b.*)$|^Easter\b")
# A remark in quotes, like "opening hours of Albert Heijn" or PH "unknown".
REMARK = re.compile(r'^(?:PH\s+)?"[^"]*"$')


@cache
def _places() -> dict[str, list[list]]:
    try:
        return json.loads(DATA.read_text(encoding="utf-8")).get("plekken", {})
    except (OSError, ValueError):
        return {}


@cache
def _holidays(year: int) -> frozenset[date]:
    """The Dutch public holidays, which OpenStreetMap calls PH."""
    # Easter, by the anonymous Gregorian algorithm.
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    easter = date(year, (h + l - 7 * m + 90) // 25, (h + l - 7 * m + 33 * ((h + l - 7 * m + 90) // 25) + 19) % 32)
    king = date(year, 4, 27) if date(year, 4, 27).weekday() != 6 else date(year, 4, 26)
    return frozenset({
        date(year, 1, 1), easter, easter + timedelta(days=1), king, date(year, 5, 5), easter + timedelta(days=39),
        easter + timedelta(days=49), easter + timedelta(days=50), date(year, 12, 25), date(year, 12, 26),
    })


def _days(selector: str) -> tuple[set[int], bool]:
    days, holiday = set(), False
    for part in re.split(r"\s*,\s*", selector):
        if part == "PH":
            holiday = True
            continue
        first, _, last = part.partition("-")
        start, end = DAYS.index(first), DAYS.index(last or first)
        days |= {(start + step) % 7 for step in range((end - start) % 7 + 1)}
    return days, holiday


@cache
def _rules(text: str) -> tuple[tuple, frozenset[int]] | None:
    """The rules of an opening_hours text as (weekdays, with holidays, spans in minutes), and the months that have
    rules for some dates only. None if it is in a form this does not read, like weeks."""
    text = " ".join(text.split())
    if text in ("24/7", "Mo-Su 00:00-24:00", "00:00-24:00"):
        return ((frozenset(range(7)), True, ((0, 1440),)),), frozenset()
    rules, months = [], set()
    for part in re.split(r"\s*;\s*|(?<=\d)\s*,\s*(?=" + DAY + r"\b)", text):
        if not part or REMARK.match(part):
            continue
        dated = DATED.match(part)
        if dated:
            named = re.findall("|".join(MONTHS), dated[1] or "")
            months |= {MONTHS.index(month) + 1 for month in named} if named else {3, 4}
            continue
        found = SELECTOR.match(part)
        days, holiday, times = (set(range(7)), True, part) if not found else (*_days(found[1]), found[2])
        times = times.strip()
        if times in ("off", "closed"):
            spans: tuple = ()
        else:
            pieces = re.split(r"\s*,\s*", times)
            matches = [SPAN.fullmatch(piece) for piece in pieces]
            if not all(matches):
                return None
            spans = []
            for match in matches:
                start = int(match[1]) * 60 + int(match[2])
                end = int(match[3]) * 60 + int(match[4])
                # A span past midnight, like 22:00-02:00, ends the next day.
                spans.append((start, end if end > start else end + 1440))
            spans = tuple(spans)
        rules.append((frozenset(days), holiday, spans))
    return (tuple(rules), frozenset(months)) if rules else None


def _spans(rules: tuple, day: date) -> tuple[tuple, bool]:
    """The open spans on a day, and whether a holiday makes them uncertain."""
    holiday = day in _holidays(day.year)
    spans: tuple = ()
    named_holiday = False
    for days, with_holiday, rule_spans in rules:
        if day.weekday() in days and (not holiday or len(days) < 7 or with_holiday):
            spans = rule_spans
        if holiday and with_holiday and len(days) < 7:
            spans, named_holiday = rule_spans, True
    return spans, holiday and not named_holiday


def _clock(minutes: int, end: bool = False) -> str:
    if end and minutes % 1440 == 0:
        return "24:00"
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _hours(spans: tuple) -> str:
    if spans == ((0, 1440),):
        return "hele dag"
    return ", ".join(f"{_clock(a)}-{_clock(b, end=True)}" for a, b in spans) or "dicht"


def _status(text: str, now: datetime) -> dict[str, Any]:
    """Open now, until when, or when it opens, from an opening_hours text."""
    read = _rules(text) if text else None
    if not read or now.month in read[1]:
        return {}
    rules = read[0]
    today = now.date()
    minute = now.hour * 60 + now.minute
    spans, unsure = _spans(rules, today)
    status: dict[str, Any] = {"vandaag": _hours(spans)}
    yesterday, _ = _spans(rules, today - timedelta(days=1))
    tomorrow, _ = _spans(rules, today + timedelta(days=1))
    current = [b for a, b in spans if a <= minute < b] + [b - 1440 for a, b in yesterday if b > 1440 and minute < b - 1440]
    if current:
        status.update(open_nu=True, open_tot=_clock(max(current), end=True))
        if spans == ((0, 1440),) and tomorrow == ((0, 1440),):
            status["open_tot"] = "dag en nacht open"
    else:
        status["open_nu"] = False
        for ahead in range(8):
            day = today + timedelta(days=ahead)
            starts = [a for a, _ in _spans(rules, day)[0] if ahead or a > minute]
            if starts:
                when = _clock(min(starts))
                status["gaat_open"] = when if not ahead else f"{'morgen' if ahead == 1 else DAY_NAMES[day.weekday()]} {when}"
                break
    if unsure:
        status["let_op"] = "Vandaag is een feestdag; de tijden kunnen anders zijn."
    return status


def _metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1) * math.cos(math.radians(lat1))
    return 6_371_000 * math.hypot(dlat, dlon)


async def _station(waar: str) -> tuple[float, float, str] | None:
    """A train station by name, from the station list of NS: 'Utrecht Centraal' or 'station Zwolle'. Only when the
    question says station or centraal, so Gouda stays the town."""
    if not re.search(r"\b(station|centraal|cs)\b", waar, re.IGNORECASE):
        return None
    wanted = re.sub(r"\b(station|ns)\b", "", waar, flags=re.IGNORECASE).strip()
    try:
        station = await ns._station(re.sub(r"\bcs$", "Centraal", wanted, flags=re.IGNORECASE))
    except (ToolError, httpx.HTTPError):
        return None
    if station.get("land") != "NL" or station.get("lat") is None:
        return None
    return station["lat"], station["lng"], f"station {ns._station_name(station)}"


async def _point(waar: str, plaats: str | None) -> tuple[float, float, str]:
    """Where to search from: a station, an address or street, or else a place or district."""
    station = await _station(" ".join(part for part in (waar, plaats) if part))
    if station:
        return station
    if plaats or _is_address(waar):
        found = await _address(waar, plaats)
        if found:
            # The street and town, without the house number that was asked.
            name = re.sub(r"\s+\d+\S*(?=,)|\s*\b\d{4}\s?[A-Z]{2}\b", "", found["naam"])
            return found["lat"], found["lon"], name
    try:
        place = await plaatsen.zoek(" ".join(part for part in (waar, plaats) if part))
    except ToolError:
        raise ToolError(
            f"Geen plek gevonden die lijkt op '{waar}'. Noem een adres of straat met de plaats, een wijk of een plaats."
        ) from None
    return place.lat, place.lon, place.naam


def misplaced(kind: str, name: str) -> bool:
    """Whether OpenStreetMap files this place under a kind it is not: a bitcoin machine, or a shop that is no
    supermarket."""
    if kind == "pinautomaat":
        return any(word in name.lower() for word in CRYPTO)
    return kind == "supermarkt" and bool(NO_SUPERMARKET.search(name)) and "super" not in name.lower()


async def open_in_de_buurt(
    waar: str, soort: str = "supermarkt", plaats: str | None = None, nu_open: bool = False, aantal: int = 5
) -> dict[str, Any]:
    kind = KINDS.get(soort.strip().lower())
    if not kind:
        raise ToolError("Ik ken alleen supermarkten, apotheken en pinautomaten. Kies supermarkt, apotheek of pinautomaat.")
    places = _places().get(kind)
    if not places:
        raise ToolError("De lijst met winkels is er even niet. Probeer het later nog eens.")
    lat, lon, name = await _point(waar, plaats)
    now = now_local().astimezone(AMSTERDAM)
    near = sorted(
        (distance, row) for row in places if (distance := _metres(lat, lon, row[0], row[1])) <= MAX_KM * 1000
    )
    found = []
    for distance, (_, _, title, brand, address, hours) in near:
        if misplaced(kind, f"{title} {brand}"):
            continue
        item: dict[str, Any] = {"naam": title or brand or kind, "adres": address or None}
        # Rounded, so the answer does not pin down the address that was asked.
        item["afstand_m"] = max(50, round(distance / 50) * 50)
        status = _status(hours, now)
        if nu_open and not status.get("open_nu"):
            continue
        item.update(status)
        if hours:
            item["openingstijden"] = hours
        found.append({key: value for key, value in item.items() if value is not None})
        if len(found) >= min(max(int(aantal), 1), MAX_ITEMS):
            break
    result: dict[str, Any] = {"rond": name, "soort": kind, "plekken": found}
    if not found:
        result["uitleg"] = (
            f"Geen {kind} gevonden binnen {MAX_KM} km" + (" die nu open is." if nu_open else ".")
        )
    result["let_op"] = LET_OP
    if kind == "apotheek":
        result["dienstapotheek"] = PHARMACY
    result["bron"] = BRON
    return result

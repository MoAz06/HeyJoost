"""Bus, tram, metro and ferry departures via Transitous, which serves the OpenOV timetables and live data.

No key needed. Transitous allows light, non-commercial use by open-source projects that send a User-Agent
with contact details and credit transitous.org/sources and OpenStreetMap.
"""

import asyncio
import math
import re
from collections.abc import Callable
from datetime import UTC, date
from difflib import SequenceMatcher
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, fmt_time, get_json, normalize, now_local, parse_moment, parse_time, place_key

BASE = "https://api.transitous.org/api"
SOURCE = "Transitous"
BRON = (
    "Dienstregeling en live-data van de vervoerders via OpenOV, opgehaald via Transitous "
    "(https://transitous.org/sources/). Haltes © OpenStreetMap-bijdragers."
)
# Without COACH: long-distance coaches like FlixBus crowd out the local lines at big stations.
MODES = "BUS,TRAM,SUBWAY,FERRY"
SOORT = {"BUS": "bus", "TRAM": "tram", "SUBWAY": "metro", "FERRY": "veerboot"}
RAIL = {"RAIL", "HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL", "REGIONAL_RAIL", "REGIONAL_FAST_RAIL", "SUBURBAN"}
DAY = 24 * 3600

# The bus and tram stops at a train station have names of their own ("Utrecht, CS Jaarbeurszijde"),
# so around a station everything within this radius counts.
STATION_RADIUS = 250
# Elsewhere the two directions of one stop can carry different town names ("Den Haag, Spui" and
# "'s-Gravenhage, Spui"), which Transitous does not join by itself.
SAME_STOP_RADIUS = 400
STATION_NAME = re.compile(r"^(station|cs)\b|\b(station|centraal|cs)$|hoofdstation$", re.IGNORECASE)
STATION_WORDS = {"station", "centraal", "hoofdstation"}
WORD_ALIASES = {"cs": ("centraal", "station"), "hs": ("hollands", "spoor")}
# A town's stops lie in its municipality, or this close to its centre (for districts like Scheveningen).
TOWN_RADIUS = 3000
# Further away than this from the town asked for, a stop is not the one meant.
NEAR_RADIUS = 15000
# Search box around a town, in degrees: about 11 km each way.
BOX_LAT, BOX_LON = 0.1, 0.16
LINE_PREFIX = re.compile(r"^(lijn|bus|tram|metro|veerboot|veerpont|pont)\s*", re.IGNORECASE)
# A word in front of the line ("tram 2"), or on its own ("metro"), also filters on the kind of transport.
LINE_KINDS = {"bus": "bus", "tram": "tram", "metro": "metro", "veerboot": "veerboot", "veerpont": "veerboot", "pont": "veerboot"}


def _short(name: str) -> str:
    """OpenOV names start with the town: 'Den Haag, Spui' becomes 'Spui'."""
    return name.rsplit(", ", 1)[-1]


def _words(text: str) -> list[str]:
    words = []
    for word in re.findall(r"\w+", text.lower()):
        words.extend(WORD_ALIASES.get(word, (word,)))
    return words


def _coverage(wanted: list[str], name: str) -> float:
    """Share of the asked words that occur in a stop name, allowing small typos."""
    words = _words(name)
    if not wanted or not words:
        return 0.0
    found = sum(1 for w in wanted if max(SequenceMatcher(None, w, word).ratio() for word in words) >= 0.8)
    return found / len(wanted)


def _towns(hit: dict) -> set[str]:
    return {place_key(name) for name in _town_names(hit)}


def _town_names(hit: dict) -> list[str]:
    names = [area.get("name", "") for area in hit.get("areas") or [] if area.get("adminLevel", 0) >= 7]
    if ", " in hit["name"]:
        names.append(hit["name"].split(", ", 1)[0])
    return [name for name in names if name]


def _town_words(hit: dict) -> set[str]:
    return {word for name in _town_names(hit) for word in _words(name)}


def _town(hit: dict) -> str | None:
    return next((area.get("name") for area in hit.get("areas") or [] if area.get("default")), None)


def _is_station(hit: dict) -> bool:
    return bool(RAIL & set(hit.get("modes") or [])) or bool(STATION_NAME.search(_short(hit["name"])))


def _distance(a: dict, b: dict) -> float:
    """Metres between two points; a flat approximation is precise enough at this scale."""
    dlat = math.radians(b["lat"] - a["lat"])
    dlon = math.radians(b["lon"] - a["lon"]) * math.cos(math.radians(a["lat"]))
    return 6_371_000 * math.hypot(dlat, dlon)


async def _geocode(text: str, kind: str, box: tuple[float, ...] | None = None) -> list[dict]:
    params: dict[str, Any] = {"text": text, "language": "nl", "type": kind, "numResults": 15}
    if kind == "STOP":
        params["mode"] = MODES
    if box:
        params["min"], params["max"] = f"{box[0]:.4f},{box[1]:.4f}", f"{box[2]:.4f},{box[3]:.4f}"
    hits = await cached(
        ("ov-geocode", kind, text.lower(), box),
        DAY,
        lambda: get_json(f"{BASE}/v1/geocode", params=params, source=SOURCE),
    )
    return [h for h in hits or [] if h.get("country") == "NL" and h.get("id") and h.get("name")]


async def _place(plaats: str) -> dict | None:
    """The centre of a town or district. Provinces with the same name, like Utrecht, are skipped."""
    key = place_key(plaats)
    hits = await _geocode(plaats, "PLACE")
    return next((h for h in hits if h.get("category") != "state" and place_key(h["name"]) == key), None)


async def _find(halte: str, plaats: str | None) -> tuple[dict, list[dict]]:
    """The stop meant, plus all stops found, best first.

    Stop names start with the town, so the first search adds the town to the text. Districts like
    Scheveningen are not in stop names, so a second search looks for the stop alone around the town.
    'Station Leiden' means Leiden Centraal rather than Station De Vink, so a last search asks for the
    town's Centraal when no main station turned up.
    """
    wanted = _words(halte)
    asked = set(wanted)
    station = bool(STATION_WORDS & asked)
    plaats_words = set(_words(plaats)) if plaats else set()
    center = await _place(plaats) if plaats else None
    box = None
    if center:
        box = (center["lat"] - BOX_LAT, center["lon"] - BOX_LON, center["lat"] + BOX_LAT, center["lon"] + BOX_LON)

    def in_town(hit: dict) -> bool:
        if not plaats:
            return True
        return place_key(plaats) in _towns(hit) or bool(center and _distance(center, hit) <= TOWN_RADIUS)

    def own_words(hit: dict) -> set[str]:
        return set(_words(hit["name"])) - _town_words(hit) - plaats_words

    def plain(hit: dict) -> bool:
        """Only station and town words were asked, as in 'Station Leiden'."""
        return not (asked - STATION_WORDS - _town_words(hit) - plaats_words)

    def main(hit: dict) -> bool:
        """The question is just 'the station' of a town, and this is that station."""
        return station and _is_station(hit) and plain(hit) and not (own_words(hit) - STATION_WORDS)

    def exact(hit: dict) -> bool:
        """Every word of the stop's own name was asked for."""
        return _coverage(wanted, hit["name"]) >= 0.5 and own_words(hit) <= asked

    def rank(hit: dict) -> tuple:
        return (in_town(hit), main(hit), exact(hit), _coverage(wanted, hit["name"]), station and _is_station(hit))

    def good(hit: dict) -> bool:
        return main(hit) or _coverage(wanted, hit["name"]) >= 0.5

    first = f"{halte} {plaats}" if plaats and normalize(plaats) not in normalize(halte) else halte
    rounds: list[tuple[str, tuple[float, ...] | None]] = [(first, None)]
    if box:
        rounds.append((halte, box))
    rest = " ".join(w for w in wanted if w not in STATION_WORDS)
    if station and (plaats or rest):
        rounds.append((f"{plaats or rest} Centraal", box))

    found: dict[str, dict] = {}
    for text, area in rounds:
        for hit in await _geocode(text, "STOP", area):
            found.setdefault(hit["id"], hit)
        # max() keeps the first of equal hits, so Transitous' own order breaks ties.
        best = max(found.values(), key=rank, default=None)
        if best and in_town(best) and (main(best) or (exact(best) and not (station and plain(best)))):
            break

    # sorted() is stable as well, so equal hits keep Transitous' order.
    hits = sorted(found.values(), key=rank, reverse=True)
    near = [h for h in hits if not center or _distance(center, h) <= NEAR_RADIUS]
    for pool in ([h for h in hits if in_town(h)], near):
        matches = [h for h in pool if good(h)]
        if matches:
            return matches[0], hits
    names = "; ".join(dict.fromkeys(h["name"] for h in (near or hits)[:5]))
    where = f" in {plaats}" if plaats else ""
    hint = f" Gevonden haltes: {names}." if names else " Controleer de naam."
    raise ToolError(f"Geen halte gevonden die lijkt op '{halte}'{where}.{hint}")


def _same_stop(best: dict, hits: list[dict]) -> list[dict]:
    """The best stop plus its namesakes close by, such as the other direction under another town name."""
    key = normalize(_short(best["name"]))
    others = [
        h for h in hits
        if h["id"] != best["id"] and normalize(_short(h["name"])) == key and _distance(best, h) <= SAME_STOP_RADIUS
    ]
    return [best, *others[:2]]


async def _stop_times(
    stop: dict, radius: int | None, n: int, when: str | None, keep: Callable[[dict], bool], enough: int
) -> list[dict]:
    params: dict[str, Any] = {"stopId": stop["id"], "n": n, "mode": MODES, "language": "nl"}
    if radius:
        params["radius"] = radius
    if when:
        params["time"] = when
    found: list[dict] = []
    # A filter on one line can need more than one page at a busy stop; three pages is the limit.
    for _ in range(3):
        query = dict(params)
        data = await cached(
            ("ov-stoptimes", tuple(sorted(query.items()))),
            30,
            lambda query=query: get_json(f"{BASE}/v6/stoptimes", params=query, source=SOURCE),
        )
        found += [st for st in data.get("stopTimes") or [] if keep(st)]
        cursor = data.get("nextPageCursor")
        if len(found) >= enough or not cursor:
            break
        params["pageCursor"] = cursor
    return found


def _row(st: dict, day: date) -> dict[str, Any]:
    place = st.get("place") or {}
    planned, actual = parse_time(place.get("scheduledDeparture")), parse_time(place.get("departure"))
    live = bool(st.get("realTime"))
    row = {
        "vertrek": fmt_time(planned),
        "verwacht": fmt_time(actual) if live else None,
        "vertraging_min": round((actual - planned).total_seconds() / 60) if live and planned and actual else 0,
        "lijn": st.get("routeShortName") or st.get("displayName"),
        "soort": SOORT.get(st.get("mode"), (st.get("mode") or "").lower()),
        "richting": st.get("headsign") or (st.get("tripTo") or {}).get("name"),
        "halte": _short(place.get("name") or ""),
        "perron": place.get("track") or place.get("scheduledTrack"),
        "vervoerder": st.get("agencyName"),
        "live": live,
        "geannuleerd": bool(st.get("cancelled") or st.get("tripCancelled")),
    }
    if planned and planned.date() != day:
        row["datum"] = planned.date().isoformat()
    return row


def _line_filter(lijn: str | None) -> tuple[str | None, str | None]:
    """Split "tram 2" into a kind and a line; "metro" has no line and "400" no kind."""
    text = (lijn or "").strip()
    match = LINE_PREFIX.match(text)
    kind = LINE_KINDS.get(match[1].lower()) if match else None
    line = text[match.end():].strip() if match else text
    return kind, line or None


def _alerts(stop_times: list[dict]) -> list[str]:
    texts = []
    for st in stop_times:
        for alert in (st.get("place") or {}).get("alerts") or []:
            text = (alert.get("headerText") or alert.get("descriptionText") or "").strip()
            if text and text not in texts:
                texts.append(text)
    return texts[:5]


async def ov_vertrek(
    halte: str,
    plaats: str | None = None,
    aantal: int = 10,
    lijn: str | None = None,
    datum: str | None = None,
    tijd: str | None = None,
) -> dict:
    halte = halte.strip()
    if not halte:
        raise ToolError("Geef de naam van een halte, bijvoorbeeld Spui of Leidseplein.")
    plaats = plaats.strip() if plaats and plaats.strip() else None
    best, candidates = await _find(halte, plaats)

    aantal = max(1, min(aantal, 25))
    moment = parse_moment(datum, tijd) if datum or tijd else None
    when = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None
    kind, line = _line_filter(lijn)
    wanted = normalize(line or "") or None
    lines_seen: set[tuple[str, str]] = set()

    def keep(st: dict) -> bool:
        if not (st.get("place") or {}).get("departure"):
            return False
        soort, name = SOORT.get(st.get("mode"), ""), st.get("routeShortName") or ""
        lines_seen.add((soort, name))
        return (wanted is None or normalize(name) == wanted) and (kind is None or soort == kind)

    if _is_station(best):
        stops, radius = [best], STATION_RADIUS
    else:
        stops, radius = _same_stop(best, candidates), None
    n = 50 if wanted or kind else aantal + 5
    batches = await asyncio.gather(*(_stop_times(s, radius, n, when, keep, aantal) for s in stops))

    seen, merged = set(), []
    for st in sorted((st for batch in batches for st in batch), key=lambda st: st["place"]["departure"]):
        key = (st.get("tripId"), st["place"].get("scheduledDeparture"))
        if key not in seen:
            seen.add(key)
            merged.append(st)
    merged = merged[:aantal]

    if not merged:
        at = f" vanaf {moment.strftime('%Y-%m-%d %H:%M')}" if moment else ""
        if wanted or kind:
            running = sorted(((s, name) for s, name in lines_seen if name), key=lambda x: (x[0], len(x[1]), x[1]))
            names = ", ".join(f"{s} {name}" for s, name in running[:15])
            hint = f" Wel bijvoorbeeld: {names}." if names else ""
            when = at or " de komende tijd"
            if wanted:
                label = f"{kind or 'lijn'} {line}"
                raise ToolError(f"{label[0].upper()}{label[1:]} vertrekt{when} niet bij {best['name']}.{hint}")
            raise ToolError(f"Er vertrekt{when} geen {kind} bij {best['name']}.{hint}")
        raise ToolError(f"Geen vertrekken gevonden bij {best['name']}{at}.")

    day = (moment or now_local()).date()
    rows = [_row(st, day) for st in merged]
    # Suggestions for when this was not the stop meant; stops already in the list are left out.
    shown = {normalize(_short(s["name"])) for s in stops} | {normalize(row["halte"]) for row in rows}
    others = [
        h["name"] for h in candidates
        if normalize(_short(h["name"])) not in shown and not (radius and _distance(best, h) <= radius)
    ]
    return {
        "halte": best["name"],
        "plaats": _town(best),
        "vertrektijden": rows,
        "meldingen": _alerts(merged),
        "andere_haltes": list(dict.fromkeys(others))[:5],
        "bron": BRON,
    }

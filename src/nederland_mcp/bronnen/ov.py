"""Bus, tram, metro and ferry departures via Transitous, which serves the OpenOV timetables and live data.

No key needed. Transitous allows light, non-commercial use by open-source projects that send a User-Agent
with contact details and credit transitous.org/sources and OpenStreetMap.

Strikes and other national messages from NS come along when they concern buses, trams or metros.
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
from . import ns

BASE = "https://api.transitous.org/api"
SOURCE = "Transitous"
LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
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
# Stop names that count as the town's main station when 'naar' asks only for "station".
MAIN_STOP_WORDS = {"centraal", "hoofdstation", "stationsplein"}
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
POSTCODE = re.compile(r"\b(\d{4})\s?([a-z]{2})\b", re.IGNORECASE)
HOUSE_NUMBER = re.compile(r"[a-z]\.?\s+(\d+)\s*[a-z]?\b", re.IGNORECASE)
ADDRESS_TYPES = ("adres", "weg", "postcode")
# Around an address, the nearest stops count up to this far, as the crow flies.
ADDRESS_RADIUS = 2000
# NS also passes on strikes and other big disruptions of buses, trams and metros. On a strike day the
# timetables, and often the live data too, still show every ride.
LOCAL_TRANSPORT = re.compile(r"\b(bus|tram|metro|streekvervoer|stadsvervoer)", re.IGNORECASE)


def _short(name: str) -> str:
    """OpenOV names start with the town: 'Den Haag, Spui' becomes 'Spui'."""
    return name.rsplit(", ", 1)[-1]


def _words(text: str) -> list[str]:
    words = []
    for word in re.findall(r"\w+", text.lower()):
        words.extend(WORD_ALIASES.get(word, (word,)))
    return words


def _coverage(wanted: list[str], name: str, strict: bool = False) -> float:
    """Share of the asked words that occur in a stop name, allowing small typos. Strict also rules out
    longer words that only start the same, such as Kurhausweg for Kurhaus."""
    words = _words(name)
    if not wanted or not words:
        return 0.0

    def close(asked: str, word: str) -> bool:
        if strict and abs(len(asked) - len(word)) > 2:
            return False
        return SequenceMatcher(None, asked, word).ratio() >= 0.8

    found = sum(1 for w in wanted if w in words or any(close(w, word) for word in words))
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


async def _find(halte: str, plaats: str | None, wide: bool = True) -> tuple[dict, list[dict], bool]:
    """The stop meant, all stops found (best first), and whether the stop's own name was asked in full.

    Stop names start with the town, so the first search adds the town to the text. Districts like
    Scheveningen are not in stop names, so a second search looks for the stop alone around the town.
    'Station Leiden' means Leiden Centraal rather than Station De Vink, so a last search asks for the
    town's Centraal when no main station turned up. Without wide, only the first search runs: for a
    street, whose own stops come next.
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
        # For a street, half the words (Prinses Margrietplantsoen for Prinses Beatrixlaan) is not enough.
        return main(hit) or _coverage(wanted, hit["name"]) >= (0.5 if wide else 1.0)

    def alike(hit: dict) -> bool:
        """A word of the stop's own name looks like an asked word, as Station Zoetermeer for Stationsplein."""
        return any(
            SequenceMatcher(None, w, x).ratio() >= 0.8 or (min(len(w), len(x)) >= 4 and (w.startswith(x) or x.startswith(w)))
            for w in asked - plaats_words
            for x in own_words(hit)
        )

    first = f"{halte} {plaats}" if plaats and normalize(plaats) not in normalize(halte) else halte
    rounds: list[tuple[str, tuple[float, ...] | None]] = [(first, None)]
    if box:
        rounds.append((halte, box))
    rest = " ".join(w for w in wanted if w not in STATION_WORDS)
    if station and (plaats or rest):
        rounds.append((f"{plaats or rest} Centraal", box))
    if not wide:
        rounds = rounds[:1]

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
    near = [h for h in hits if wide and (not center or _distance(center, h) <= NEAR_RADIUS)]
    for pool in ([h for h in hits if in_town(h)], near):
        matches = [h for h in pool if good(h)]
        if matches:
            return matches[0], hits, main(matches[0]) or exact(matches[0])
    names = "; ".join(list(dict.fromkeys(h["name"] for h in (near or hits) if alike(h)))[:5])
    where = f" in {plaats}" if plaats else ""
    # Only the names look alike, so these stops can be anywhere in town.
    hint = f" Haltes met een naam die erop lijkt, niet per se in de buurt: {names}." if names else " Controleer de naam."
    raise ToolError(f"Geen halte of adres gevonden dat lijkt op '{halte}'{where}.{hint}")


def _same_stop(best: dict, hits: list[dict]) -> list[dict]:
    """The best stop plus its namesakes close by, such as the other direction under another town name."""
    key = normalize(_short(best["name"]))
    others = [
        h for h in hits
        if h["id"] != best["id"] and normalize(_short(h["name"])) == key and _distance(best, h) <= SAME_STOP_RADIUS
    ]
    return [best, *others[:2]]


def _is_address(text: str) -> bool:
    """A postcode or a house number makes it an address rather than a stop name."""
    return bool(POSTCODE.search(text) or HOUSE_NUMBER.search(text))


def _names_street(text: str, street: str, fuzzy: bool = True) -> bool:
    """The text names this street, allowing a small typo: 'Laan v Meerdervoort 100' names Laan van Meerdervoort."""
    wanted = normalize(street)
    words = text.split()
    for start in range(len(words)):
        for end in range(start + 1, len(words) + 1):
            span = normalize(" ".join(words[start:end]))
            if span == wanted or (fuzzy and len(span) > 3 and SequenceMatcher(None, span, wanted).ratio() >= 0.85):
                return True
    return False


async def _address(text: str, plaats: str | None, fuzzy: bool = True) -> dict | None:
    """Where an address, street or postcode is, from PDOK. None when nothing matches."""
    query = re.sub(r"[^\w\s'.-]", " ", f"{text} {plaats or ''}").strip()
    params = [
        ("q", query),
        ("fq", f"type:({' OR '.join(ADDRESS_TYPES)})"),
        ("fl", "type,weergavenaam,straatnaam,postcode,woonplaatsnaam,gemeentenaam,centroide_ll"),
        ("rows", "10"),
    ]
    try:
        data = await cached(
            ("ov-adres", query.lower()),
            DAY,
            lambda: get_json(LOCATIESERVER, params=params, source="PDOK Locatieserver"),
        )
    except ToolError:
        # A street is only checked next to the stop search, which can do without it.
        if _is_address(text):
            raise
        return None
    code = POSTCODE.search(text)
    code = f"{code[1]}{code[2]}".upper() if code else None

    def fits(doc: dict) -> bool:
        if plaats and place_key(plaats) not in (place_key(doc.get("woonplaatsnaam") or ""), place_key(doc.get("gemeentenaam") or "")):
            return False
        if code and doc.get("postcode") != code:
            return False
        return bool(code and doc.get("type") == "postcode") or _names_street(text, doc.get("straatnaam") or "", fuzzy)

    docs = [d for d in data.get("response", {}).get("docs", []) if d.get("centroide_ll") and fits(d)]
    if not docs:
        return None
    towns = list(dict.fromkeys(d.get("woonplaatsnaam") for d in docs if d.get("woonplaatsnaam")))
    if not plaats and not code and len(towns) > 1:
        raise ToolError(f"'{text}' bestaat in meer dan één plaats, bijvoorbeeld: {', '.join(towns[:5])}. Geef de plaats mee.")
    lon, lat = (float(x) for x in docs[0]["centroide_ll"].removeprefix("POINT(").removesuffix(")").split())
    return {"lat": lat, "lon": lon, "naam": docs[0]["weergavenaam"]}


async def _near(point: dict) -> list[dict]:
    """The stops nearest to a point, one per name, nearest first. Transitous gets the point rounded to
    about a hundred metres, never the address itself."""
    place = f"{point['lat']:.3f},{point['lon']:.3f}"
    hits = await cached(
        ("ov-near", place),
        DAY,
        lambda: get_json(f"{BASE}/v1/reverse-geocode", params={"place": place, "type": "STOP"}, source=SOURCE),
    )
    stops = [h for h in hits or [] if h.get("country") == "NL" and h.get("id") and h.get("name")]
    return sorted((h for h in stops if _distance(point, h) <= ADDRESS_RADIUS), key=lambda h: _distance(point, h))


async def _origin(halte: str, plaats: str | None) -> tuple[dict | None, dict | None, list[dict]]:
    """Where to leave from: an address or street as (point, None, []), or a stop as (None, stop, candidates).

    A stop can be named after its street (Spui), so for a street the stop search still runs. With a town,
    the street is looked up first and then only the first round of the stop search runs: that keeps the
    lookups at Transitous within the one to three per question they were told.
    """
    if _is_address(halte):
        point = await _address(halte, plaats)
        if point:
            return point, None, []
        best, candidates, _ = await _find(halte, plaats)
        return None, best, candidates
    street = await _address(halte, plaats) if plaats else None
    try:
        best, candidates, sure = await _find(halte, plaats, wide=street is None)
    except ToolError:
        street = street or (None if plaats else await _address(halte, None, fuzzy=False))
        if street is None:
            raise
        return street, None, []
    # Without a town, a street can look like a stop elsewhere, as Rotterdam's Van Noortwijckstraat for the
    # Paets van Troostwijkstraat in Den Haag. A street with exactly the name asked wins then.
    if not sure and not plaats:
        street = await _address(halte, None, fuzzy=False)
        if street:
            return street, None, []
    return None, best, candidates


def _meters(a: dict, b: dict) -> int:
    return round(_distance(a, b) / 10) * 10


def _kinds(stop: dict) -> list[str]:
    modes = stop.get("modes") or []
    return [SOORT[m] for m in modes if m in SOORT] + (["trein"] if RAIL & set(modes) else [])


def _without_town(wanted: list[str], town: str) -> list[str] | None:
    """The asked words minus the stop's town, also when written differently (Den Haag, 's-Gravenhage)."""
    key = place_key(town)
    for start in range(len(wanted)):
        for end in range(start + 1, len(wanted) + 1):
            if place_key(" ".join(wanted[start:end])) == key:
                return wanted[:start] + wanted[end:]
    return None


def _is_target(wanted: list[str], name: str) -> bool:
    """The stop name answers 'naar': the asked words besides the town are in its own name."""
    town, own = name.split(", ", 1) if ", " in name else ("", name)
    rest = _without_town(wanted, town) if town else None
    if rest is None:
        rest = wanted
    if not rest:
        return True
    own_words = set(_words(own))
    # Only station words asked, as in "station Leiden" or "Leiden Centraal": the town's main station.
    # "Centraal" has to be in the name then, so Hoofddorp's plain "Station" is no Centraal Station.
    if set(rest) <= STATION_WORDS:
        main = set(rest) - {"station"}
        if main:
            return bool(own_words & {"centraal", "hoofdstation"})
        return bool(own_words & MAIN_STOP_WORDS) or own_words == {"station"}
    if _coverage([w for w in rest if w != "station"], own, strict=True) == 1.0:
        return True
    # "Kurhaus Scheveningen" still means the stop Kurhaus: every word of its own name was asked.
    named = list(own_words - STATION_WORDS)
    return (
        bool(named)
        and _coverage(named, " ".join(rest), strict=True) == 1.0
        and _coverage(rest, own, strict=True) >= 0.5
    )


def _target_stop(st: dict, wanted: list[str], memo: dict[str, bool]) -> dict | None:
    """The first later stop of this trip that answers 'naar'. A place like Scheveningen is in no stop
    name but can be in the headsign (Scheveningen Noord), and then the last stop counts."""
    stops = st.get("nextStops") or []
    for stop in stops:
        name = stop.get("name") or ""
        if name not in memo:
            memo[name] = _is_target(wanted, name)
        if memo[name]:
            return stop
    if stops and _coverage(wanted, st.get("headsign") or "", strict=True) == 1.0:
        return stops[-1]
    return None


async def _stop_times(
    stop: dict,
    radius: int | None,
    n: int,
    when: str | None,
    keep: Callable[[dict], bool],
    enough: int,
    pages: int,
    next_stops: bool = False,
) -> list[dict]:
    params: dict[str, Any] = {"stopId": stop["id"], "n": n, "mode": MODES, "language": "nl"}
    if radius:
        params["radius"] = radius
    # The stops after each departure make the answer about five times bigger, so only when 'naar' needs them.
    if next_stops:
        params["fetchStops"] = "true"
    if when:
        params["time"] = when
    found: list[dict] = []
    # A filter can need more than one page at a busy stop.
    for _ in range(pages):
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


def _row(st: dict, day: date, target: dict | None = None) -> dict[str, Any]:
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
    if target is not None:
        arrival, expected = parse_time(target.get("scheduledArrival")), parse_time(target.get("arrival"))
        row["stopt_bij"] = target.get("name")
        row["aankomst"] = fmt_time(arrival)
        row["aankomst_verwacht"] = fmt_time(expected) if live else None
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


async def _notices() -> list[str]:
    return [text for text in await ns.berichten() if LOCAL_TRANSPORT.search(text)]


async def ov_vertrek(
    halte: str,
    plaats: str | None = None,
    aantal: int = 10,
    lijn: str | None = None,
    datum: str | None = None,
    tijd: str | None = None,
    naar: str | None = None,
) -> dict:
    halte = halte.strip()
    if not halte:
        raise ToolError("Geef de naam van een halte of een adres, bijvoorbeeld Spui of Laan van Meerdervoort 100.")
    plaats = plaats.strip() if plaats and plaats.strip() else None
    naar = naar.strip() if naar and naar.strip() else None
    # Around an address or a street, the stops nearby count.
    point, best, candidates = await _origin(halte, plaats)
    if point:
        candidates = await _near(point)
        if not candidates:
            raise ToolError(f"Geen halte voor bus, tram of metro gevonden binnen 2 km van {point['naam']}.")
        best = candidates[0]

    aantal = max(1, min(aantal, 25))
    moment = parse_moment(datum, tijd) if datum or tijd else None
    when = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None
    kind, line = _line_filter(lijn)
    wanted = normalize(line or "") or None
    target = (_words(naar) or None) if naar else None
    lines_seen: set[tuple[str, str]] = set()
    headsigns: list[str] = []
    memo: dict[str, bool] = {}
    passed = 0

    def keep(st: dict) -> bool:
        nonlocal passed
        if not (st.get("place") or {}).get("departure"):
            return False
        soort, name = SOORT.get(st.get("mode"), ""), st.get("routeShortName") or ""
        lines_seen.add((soort, name))
        if not ((wanted is None or normalize(name) == wanted) and (kind is None or soort == kind)):
            return False
        passed += 1
        if target is None:
            return True
        if st.get("headsign") and st["headsign"] not in headsigns:
            headsigns.append(st["headsign"])
        return _target_stop(st, target, memo) is not None

    if point:
        # The three nearest stops, a station with the stops around it.
        stops = candidates[:3]
        radii = [STATION_RADIUS if _is_station(s) else None for s in stops]
    elif _is_station(best):
        stops, radii = [best], [STATION_RADIUS]
    else:
        stops = _same_stop(best, candidates)
        radii = [None] * len(stops)
    n = 50 if wanted or kind or target else aantal + 5
    # Transitous was told 1 to 3 stoptimes calls per question, so the stops share three pages.
    pages = max(1, 3 // len(stops))
    batches, notices = await asyncio.gather(
        asyncio.gather(*(
            _stop_times(s, radius, n, when, keep, aantal, pages, next_stops=target is not None)
            for s, radius in zip(stops, radii)
        )),
        _notices(),
    )

    # One trip can call at two stops around a station (Leiden Centraal and its Westzijde); the first counts.
    # Trip ids start with the date and time, so a trip on another day is another id.
    seen, merged = set(), []
    for st in sorted((st for batch in batches for st in batch), key=lambda st: st["place"]["departure"]):
        key = st.get("tripId") or (st.get("routeShortName"), st["place"].get("scheduledDeparture"))
        if key not in seen:
            seen.add(key)
            merged.append(st)
    merged = merged[:aantal]

    if not merged:
        at = f" vanaf {moment.strftime('%Y-%m-%d %H:%M')}" if moment else ""
        period = at or " de komende tijd"
        where = f"de haltes bij {point['naam']} ({', '.join(_short(s['name']) for s in stops)})" if point else best["name"]
        # A strike can be why nothing runs.
        warning = "".join(f" Let op: {text}" for text in notices)
        if target and passed:
            hint = f" Wel richtingen: {', '.join(headsigns[:8])}." if headsigns else ""
            if STATION_WORDS & set(target):
                hint += " Verder met de trein? Zoek dan de bus of tram naar een station in de buurt, en de trein met trein_reis."
            raise ToolError(f"Er vertrekt{period} niks van {where} dat bij '{naar}' stopt.{hint}{warning}")
        if wanted or kind:
            running = sorted(((s, name) for s, name in lines_seen if name), key=lambda x: (x[0], len(x[1]), x[1]))
            names = ", ".join(f"{s} {name}" for s, name in running[:15])
            hint = f" Wel bijvoorbeeld: {names}." if names else ""
            if wanted:
                label = f"{kind or 'lijn'} {line}"
                raise ToolError(f"{label[0].upper()}{label[1:]} vertrekt{period} niet bij {where}.{hint}{warning}")
            raise ToolError(f"Er vertrekt{period} geen {kind} bij {where}.{hint}{warning}")
        raise ToolError(f"Geen vertrekken gevonden bij {where}{at}.{warning}")

    day = (moment or now_local()).date()
    rows = [_row(st, day, _target_stop(st, target, memo) if target else None) for st in merged]
    # First in the result, so it is read before the times.
    warning = {"let_op": notices} if notices else {}
    bron = f"{BRON} De berichten in let_op komen van NS Reisinformatie." if notices else BRON
    if point:
        for row, st in zip(rows, merged):
            row["afstand_m"] = _meters(point, st["place"])
        nearby = [{"halte": s["name"], "afstand_m": _meters(point, s), "soort": _kinds(s)} for s in stops]
        return {
            **warning,
            "adres": point["naam"],
            "haltes_in_de_buurt": nearby,
            "vertrektijden": rows,
            "meldingen": _alerts(merged),
            "bron": bron,
        }
    # Suggestions for when this was not the stop meant; stops already in the list are left out.
    shown = {normalize(_short(s["name"])) for s in stops} | {normalize(row["halte"]) for row in rows}
    others = [
        h["name"] for h in candidates
        if normalize(_short(h["name"])) not in shown and not (radii[0] and _distance(best, h) <= radii[0])
    ]
    return {
        **warning,
        "halte": best["name"],
        "plaats": _town(best),
        "vertrektijden": rows,
        "meldingen": _alerts(merged),
        "andere_haltes": list(dict.fromkeys(others))[:5],
        "bron": bron,
    }

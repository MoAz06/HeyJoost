"""Bus, tram, metro and ferry departures via Transitous, which serves the OpenOV timetables and live data.

No key needed. Transitous allows light, non-commercial use by open-source projects that send a User-Agent
with contact details and credit transitous.org/sources and OpenStreetMap.

Strikes and other national messages from NS come along when they concern buses, trams or metros.
"""

import asyncio
import math
import re
from collections.abc import Callable
from datetime import UTC, date, timedelta
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
# A place that only lends its name to one stop, like the Erasmus MC to Erasmus MC Hoofdingang, is also served
# by the stops within this walk of it (Dijkzigt, Kievitslaan), both to leave from and to go to.
PLACE_RADIUS = 400
# A walk between such stops, or on to the place: a third longer than as the crow flies, at 80 metres a minute.
DETOUR, WALK_SPEED = 1.3, 80
# A place that no stop is named after, like Rotterdam Ahoy, is found by the place search, and is bigger than a
# stop: the stops within this walk of it serve it.
SPOT_RADIUS = 600
# Words a place's name can have or leave out: De Uithof is the Uithof.
ARTICLES = {"de", "het", "t"}
# The place search also finds what is only named after a place, like its car parks, which are not where it is,
# and what is only part of it, like an LUMC office a kilometre from the hospital.
BESIDE = ("parking", "charging_station", "fuel")
MINOR = ("office", "shop", "restaurant", "cafe", "pub", "bar", "fast_food", "hotel")
STATION_NAME = re.compile(r"^(station|cs)\b|\b(station|centraal|cs)$|hoofdstation$", re.IGNORECASE)
STATION_WORDS = {"station", "centraal", "hoofdstation"}
# Stop names that count as the town's main station when 'naar' asks only for "station".
MAIN_STOP_WORDS = {"centraal", "hoofdstation", "stationsplein"}
# Short forms in stop names, as Arnhem's "Centraal Stat.(Sonsbeek)".
WORD_ALIASES = {"cs": ("centraal", "station"), "hs": ("hollands", "spoor"), "stat": ("station",)}
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


def _lookalike(wanted: list[str], name: str) -> bool:
    """An asked word is not in the name but only looks like one of its words, as Hofplein and P.C. Hooftplein."""
    words = set(_words(name))
    return any(
        w not in words and any(SequenceMatcher(None, w, word).ratio() >= 0.8 for word in words)
        for w in set(wanted) - STATION_WORDS
    )


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

    # A town in the name asked counts as well: Utrecht Science Park is not Amsterdam Science Park. The hits
    # tell which of the asked words are towns.
    towns_named: set[str] = set()

    def named_town(hit: dict) -> bool:
        return not towns_named or bool(towns_named & _town_words(hit))

    def rank(hit: dict) -> tuple:
        return (
            in_town(hit), named_town(hit), main(hit), exact(hit), _coverage(wanted, hit["name"]), station and _is_station(hit)
        )

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
        towns_named = asked & {word for hit in found.values() for word in _town_words(hit)}
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
            # Other stops only from the town asked for: no Amsterdam Centraal for Rotterdam Centraal.
            others = [h for h in hits if in_town(h) and named_town(h)]
            return matches[0], others, main(matches[0]) or exact(matches[0])
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


async def _spots(text: str, plaats: str | None = None, box: tuple[float, ...] | None = None) -> list[dict]:
    """Places with exactly this name that no stop is named after, like the LUMC, Rotterdam Ahoy or the Johan
    Cruijff ArenA, from the same search as the stops. A main entrance comes first (LUMC Hoofdingang), then the
    place itself, then an office or a shop with just its name."""
    plaats_words = set(_words(plaats)) if plaats else set()
    wanted = set(_words(text)) - ARTICLES - plaats_words
    center = await _place(plaats) if plaats else None
    if center:
        box = (center["lat"] - BOX_LAT, center["lon"] - BOX_LON, center["lat"] + BOX_LAT, center["lon"] + BOX_LON)

    def in_town(hit: dict) -> bool:
        return not plaats or place_key(plaats) in _towns(hit) or bool(center and _distance(center, hit) <= TOWN_RADIUS)

    # As for stops, first with the town in the text, then the name alone around the town: the first finds
    # Rotterdam Ahoy among its car parks, the second the LUMC.
    rounds = [(f"{text} {plaats}", None), (text, box)] if plaats else [(text, box)]
    for query, area in rounds:
        try:
            hits = await _geocode(query, "PLACE", area)
        except ToolError:
            return []
        tiers: dict[int, list[dict]] = {}
        for hit in hits:
            category, town = hit.get("category") or "", _town_words(hit)
            asked = wanted - town
            words = set(_words(hit["name"])) - ARTICLES - town - plaats_words
            if not asked or category.startswith(BESIDE) or not in_town(hit):
                continue
            if category.startswith("entrance") and asked <= words:
                tiers.setdefault(0, []).append(hit)
            elif words == asked:
                tiers.setdefault(2 if category.startswith(MINOR) else 1, []).append(hit)
        if tiers:
            # The name alone before the name with its town: Hofplein before Rotterdam Hofplein.
            return sorted(tiers[min(tiers)], key=lambda hit: normalize(hit["name"]) != normalize(text))
    return []


async def _spot(text: str, plaats: str | None) -> dict | None:
    """A place that no stop is named after, as a point like an address: the main entrance of the LUMC, or the
    Johan Cruijff ArenA itself. None when no place has exactly this name."""
    spots = await _spots(text, plaats)
    towns = list(dict.fromkeys(town for town in map(_town, spots) if town))
    if len(towns) > 1:
        raise ToolError(f"'{text}' bestaat in meer dan één plaats, bijvoorbeeld: {', '.join(towns[:5])}. Geef de plaats mee.")
    if not spots:
        return None
    spot = spots[0]
    name = spot["name"] if not towns or normalize(towns[0]) in normalize(spot["name"]) else f"{spot['name']}, {towns[0]}"
    return {"lat": spot["lat"], "lon": spot["lon"], "naam": name}


async def _origin(halte: str, plaats: str | None) -> tuple[dict | None, dict | None, list[dict], bool]:
    """Where to leave from: an address or street as (point, None, [], True), or a stop as (None, stop,
    candidates, sure), where sure says that the stop's own name was asked in full.

    A stop can be named after its street (Spui), so for a street the stop search still runs. With a town,
    the street is looked up first and then only the first round of the stop search runs: that keeps the
    lookups at Transitous within the one to three per question they were told. A place that no stop is
    named after, like the LUMC or Rotterdam Ahoy, counts as an address too.
    """
    if _is_address(halte):
        point = await _address(halte, plaats)
        if point:
            return point, None, [], True
        best, candidates, sure = await _find(halte, plaats)
        return None, best, candidates, sure
    street = await _address(halte, plaats) if plaats else None
    try:
        best, candidates, sure = await _find(halte, plaats, wide=street is None)
    except ToolError:
        street = street or (None if plaats else await _address(halte, None, fuzzy=False))
        street = street or await _spot(halte, plaats)
        if street is None:
            raise
        return street, None, [], True
    # Without a town, a street can look like a stop elsewhere, as Rotterdam's Van Noortwijckstraat for the
    # Paets van Troostwijkstraat in Den Haag. A street with exactly the name asked wins then, but only in the
    # town that name has in it: Utrecht Science Park is not the street Science Park in Amsterdam.
    town = plaats or (_town(best) if set(_words(halte)) & _town_words(best) else None)
    if not sure and not plaats:
        street = await _address(halte, town, fuzzy=False)
        if street:
            return street, None, [], True
    # A stop that only looks like the name asked, as P.C. Hooftplein for Hofplein, gives way to a street or a
    # place with exactly that name.
    if _lookalike(_words(halte), best["name"]):
        street = await _address(halte, town, fuzzy=False) or await _spot(halte, town)
        if street:
            return street, None, [], True
    return None, best, candidates, sure


def _meters(a: dict, b: dict) -> int:
    return round(_distance(a, b) / 10) * 10


def _walk(metres: float) -> int:
    """Minutes on foot, at least one."""
    return max(1, math.ceil(metres * DETOUR / WALK_SPEED))


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
        # Besides those the name may only repeat the town, give a side of the station (CS Centrumzijde, Leiden
        # Centraal Westzijde) or add something in brackets ("(uitstaphalte)"): Centraal Museum is no station.
        words = _words(re.sub(r"\(.*?\)", " ", own))
        without = _without_town(words, town) if town else None
        name_words = set(words if without is None else without)
        if any(w not in STATION_WORDS | MAIN_STOP_WORDS and not w.endswith("zijde") for w in name_words):
            return False
        main = set(rest) - {"station"}
        if main:
            return bool(name_words & {"centraal", "hoofdstation"})
        return bool(name_words & MAIN_STOP_WORDS) or name_words == {"station"}
    if _coverage([w for w in rest if w != "station"], own, strict=True) == 1.0:
        return True
    # "Kurhaus Scheveningen" still means the stop Kurhaus: every word of its own name was asked.
    named = list(own_words - STATION_WORDS)
    return (
        bool(named)
        and _coverage(named, " ".join(rest), strict=True) == 1.0
        and _coverage(rest, own, strict=True) >= 0.5
    )


async def _places(wanted: list[str], naar: str, origin: dict) -> list[dict]:
    """The stops around the origin that 'naar' lends its name to when it is a place rather than a stop, like
    Erasmus MC Hoofdingang for the Erasmus MC, or the place itself when no stop has its name, like Rotterdam
    Ahoy. Empty when 'naar' is the name of a stop itself, or a town or its station, because then the name says
    which stops are meant."""
    if not set(wanted) - STATION_WORDS:
        return []
    box = (origin["lat"] - BOX_LAT, origin["lon"] - BOX_LON, origin["lat"] + BOX_LAT, origin["lon"] + BOX_LON)
    try:
        hits = await _geocode(naar, "STOP", box)
    except ToolError:
        # The stop names still count without it.
        return []
    # A stop that only looks like the name, as P.C. Hooftplein for Hofplein, is not named after it.
    named = [h for h in hits if _is_target(wanted, h["name"]) and not _lookalike(wanted, h["name"])]
    for hit in named:
        town = _town_words(hit)
        if set(_words(hit["name"])) - town <= set(wanted) or set(wanted) - town <= STATION_WORDS:
            return []
    if named:
        return named
    spots = await _spots(naar, box=box)
    if not spots:
        return []
    # One place can have more than one point (Rotterdam Ahoy has two), but another place with that name, like
    # De Binnenhof in Leidschendam for the Binnenhof in Den Haag, lies further from the origin.
    nearest = min(spots, key=lambda spot: _distance(origin, spot))
    return [spot for spot in spots if _distance(nearest, spot) <= 2 * SPOT_RADIUS]


def _reach(place: dict) -> int:
    """How far a stop may be from what 'naar' stands for: from a place like Rotterdam Ahoy further than from
    the stop that a place lends its name to."""
    return SPOT_RADIUS if place.get("type") == "PLACE" else PLACE_RADIUS


def _within(places: list[dict], stop: dict) -> bool:
    return any(_distance(place, stop) <= _reach(place) for place in places)


def _from(places: list[dict], point: dict) -> float:
    return min(_distance(place, point) for place in places)


def _target_stop(
    st: dict, wanted: list[str], memo: dict[str, bool], places: list[dict], origin: dict | None = None
) -> dict | None:
    """The first later stop of this trip that answers 'naar'. A place like Scheveningen is in no stop
    name but can be in the headsign (Scheveningen Noord), and then the last stop counts. Around a place
    that lends its name to a stop, or that no stop is named after, the stop nearest to it within a short walk
    counts too, when it is nearer than where you start and where you get on."""
    stops = st.get("nextStops") or []
    # When a place has the very name asked, a stop whose name only looks like it is somewhere else.
    strict = any(place.get("type") == "PLACE" for place in places)
    for stop in stops:
        name = stop.get("name") or ""
        if name not in memo:
            memo[name] = _is_target(wanted, name) and not (strict and _lookalike(wanted, name))
        if memo[name]:
            return stop
    if stops and _coverage(wanted, st.get("headsign") or "", strict=True) == 1.0:
        return stops[-1]
    if not places:
        return None
    # From Rotterdam Centraal, tram 3 to Kruisplein only takes you further from the Hofplein, and from Dijkzigt,
    # tram 14 round the corner to 's-Gravendijkwal further from the Erasmus MC.
    here = [point for point in (origin, st.get("place")) if point and "lat" in point]
    limit = min((_from(places, point) for point in here), default=math.inf)
    close = [stop for stop in stops if "lat" in stop and _within(places, stop) and _from(places, stop) < limit]
    return min(close, key=lambda stop: _from(places, stop), default=None)


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
        # Around a station or a place one trip can call at several of the stops, so it counts once.
        if len({_trip(st) for st in found}) >= enough or not cursor:
            break
        params["pageCursor"] = cursor
    return found


def _trip(st: dict) -> tuple | str:
    """Trip ids start with the date and time, so a trip on another day is another id."""
    return st.get("tripId") or (st.get("routeShortName"), (st.get("place") or {}).get("scheduledDeparture"))


def _row(st: dict, day: date, target: dict | None = None, walk: int | None = None) -> dict[str, Any]:
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
        # The last stretch on foot, worked out here rather than by the reader.
        there = expected if live and expected else arrival
        if walk is not None and there:
            row["lopen_min"] = walk
            row["op_bestemming"] = fmt_time(there + timedelta(minutes=walk))
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
    point, best, candidates, sure = await _origin(halte, plaats)
    if point:
        candidates = await _near(point)
        if not candidates:
            raise ToolError(f"Geen halte voor bus, tram of metro gevonden binnen 2 km van {point['naam']}.")
        best = candidates[0]
    # A stop whose own name was not asked in full stands for a place, like Erasmus MC Hoofdingang for the
    # Erasmus MC, so the stops around it count too. They also do on the way to somewhere, because what goes
    # there can leave from around the corner: bus 44 to Rotterdam Centraal stops at Dijkzigt, not at the
    # Hoofdingang.
    around = not point and not _is_station(best) and (not sure or bool(naar))

    aantal = max(1, min(aantal, 25))
    moment = parse_moment(datum, tijd) if datum or tijd else None
    when = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None
    kind, line = _line_filter(lijn)
    wanted = normalize(line or "") or None
    target = (_words(naar) or None) if naar else None
    places = await _places(target, naar, point or best) if target and naar else []
    # A place within a walk of where you are, as the Hofplein from Rotterdam Centraal: how long that walk is.
    on_foot = _walk(_from(places, point or best)) if places and _within(places, point or best) else None
    walking = f"ongeveer {on_foot} {'minuut' if on_foot == 1 else 'minuten'}" if on_foot else ""
    lines_seen: set[tuple[str, str]] = set()
    headsigns: list[str] = []
    memo: dict[str, bool] = {}
    passed = slow = 0
    start = moment or now_local()
    own_name = normalize(_short(best["name"]))

    def walk_to(place: dict) -> int:
        """Minutes on foot from the stop asked to the one round the corner that a ride leaves from; none to
        the stop itself or its namesake across the road."""
        if not around or "lat" not in place or normalize(_short(place.get("name") or "")) == own_name:
            return 0
        return _walk(_distance(best, place))

    def last_walk(stop: dict) -> int | None:
        """Minutes on foot from a stop found by the walk rather than the name on to the place."""
        if places and "lat" in stop and not memo.get(stop.get("name") or "") and _within(places, stop):
            return _walk(_from(places, stop))
        return None

    def keep(st: dict) -> bool:
        nonlocal passed, slow
        place = st.get("place") or {}
        if not place.get("departure"):
            return False
        # From a stop round the corner, only what still leaves once you have walked there.
        walk, leaves = walk_to(place), parse_time(place["departure"])
        if walk and not (leaves and leaves >= start + timedelta(minutes=walk)):
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
        stop = _target_stop(st, target, memo, places, point or best)
        if stop is None:
            return False
        # Within a walk, a ride that gets you there later than walking straight there is no use: from Rotterdam
        # Centraal, bus 38 to Noordsingel is at the Hofplein at 10:19, the walk takes 6 minutes.
        arrival = parse_time(stop.get("arrival")) or parse_time(stop.get("scheduledArrival"))
        if on_foot and leaves and arrival:
            there = arrival + timedelta(minutes=last_walk(stop) or 0)
            if there > leaves + timedelta(minutes=on_foot - walk):
                slow += 1
                return False
        return True

    if point:
        # The three nearest stops, a station with the stops around it.
        stops = candidates[:3]
        radii = [STATION_RADIUS if _is_station(s) else None for s in stops]
    elif _is_station(best):
        stops, radii = [best], [STATION_RADIUS]
    elif around:
        stops, radii = [best], [PLACE_RADIUS]
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
    # Around a place the nearest counts, as Erasmus MC Hoofdingang rather than Breitnerstraat for bus 32.
    departures = sorted((st for batch in batches for st in batch), key=lambda st: st["place"]["departure"])
    if around:
        departures.sort(key=lambda st: _distance(best, st["place"]))
    seen, merged = set(), []
    for st in departures:
        if _trip(st) not in seen:
            seen.add(_trip(st))
            merged.append(st)
    merged = sorted(merged, key=lambda st: st["place"]["departure"])[:aantal]

    if not merged:
        at = f" vanaf {moment.strftime('%Y-%m-%d %H:%M')}" if moment else ""
        period = at or " de komende tijd"
        if point:
            where = f"de haltes bij {point['naam']} ({', '.join(_short(s['name']) for s in stops)})"
        else:
            where = f"de haltes rond {best['name']}" if around else best["name"]
        # A strike can be why nothing runs.
        warning = "".join(f" Let op: {text}" for text in notices)
        if target and passed:
            if slow:
                raise ToolError(f"Lopen naar {naar} is sneller dan wat er{period} van {where} heen gaat: dat is {walking}.{warning}")
            hint = f" Wel richtingen: {', '.join(headsigns[:8])}." if headsigns else ""
            if STATION_WORDS & set(target):
                hint += " Verder met de trein? Zoek dan de bus of tram naar een station in de buurt, en de trein met trein_reis."
            if on_foot:
                hint += f" Lopen kan wel: dat is {walking}."
            reach = " of op loopafstand ervan" if places else ""
            raise ToolError(f"Er vertrekt{period} niks van {where} dat bij '{naar}'{reach} stopt.{hint}{warning}")
        if wanted or kind:
            running = sorted(((s, name) for s, name in lines_seen if name), key=lambda x: (x[0], len(x[1]), x[1]))
            names = ", ".join(f"{s} {name}" for s, name in running[:15])
            hint = f" Wel bijvoorbeeld: {names}." if names else ""
            if wanted:
                label = f"{kind or 'lijn'} {line}"
                raise ToolError(f"{label[0].upper()}{label[1:]} vertrekt{period} niet bij {where}.{hint}{warning}")
            raise ToolError(f"Er vertrekt{period} geen {kind} bij {where}.{hint}{warning}")
        raise ToolError(f"Geen vertrekken gevonden bij {where}{at}.{warning}")

    day = start.date()
    rows = []
    for st in merged:
        stop = _target_stop(st, target, memo, places, point or best) if target else None
        row = _row(st, day, stop, last_walk(stop) if stop is not None else None)
        # How far a stop is that was not asked for by name: one round the corner, or any around a place.
        if point:
            row["afstand_m"] = _meters(point, st["place"])
        elif around and (walk_to(st["place"]) or not sure):
            row["afstand_m"] = _meters(best, st["place"])
        rows.append(row)
    # First in the result, so they are read before the times.
    first = {"let_op": notices} if notices else {}
    # No stop on the way carries the name asked, which reads as if nothing goes there, so say how it does.
    if any("lopen_min" in row for row in rows):
        first["uitstappen"] = (
            f"Naar {naar} stap je uit bij de halte in 'stopt_bij'. Staat er 'lopen_min' bij, dan loop je daarna "
            "nog zoveel minuten en ben je er om 'op_bestemming'."
        )
    if on_foot:
        first["lopen"] = f"Lopen naar {naar} kan ook: dat is {walking}."
    bron = f"{BRON} De berichten in let_op komen van NS Reisinformatie." if notices else BRON
    if point:
        nearby = [{"halte": s["name"], "afstand_m": _meters(point, s), "soort": _kinds(s)} for s in stops]
        return {
            **first,
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
        **first,
        "halte": best["name"],
        "plaats": _town(best),
        "vertrektijden": rows,
        "meldingen": _alerts(merged),
        "andere_haltes": list(dict.fromkeys(others))[:5],
        "bron": bron,
    }

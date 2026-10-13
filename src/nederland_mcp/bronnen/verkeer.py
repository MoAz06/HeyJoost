"""Traffic on the road now: jams, accidents, broken-down cars, closed roads, exits and lanes, roadworks and open
bridges, from the current traffic picture of NDW (Nationaal Dataportaal Wegverkeer, open data under CC0).

The picture covers the motorways and many roads of provinces and municipalities, and NDW renews it every minute. It
is unvalidated: the road authorities send it, and not all of them send everything. Road numbers and the names of exits
come from VILD, NDW's location table (data/vild.json, made by scripts/ndw_vild.py). A message without such a code only
has coordinates; its street comes from PDOK.
"""

import asyncio
import gzip
import json
import logging
import math
import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from functools import cache
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, fmt_datetime, get_bytes, get_json, now_local, parse_time
from . import plaats as plaatsen

log = logging.getLogger(__name__)

URL = "https://opendata.ndw.nu/actueel_beeld.xml.gz"
REVERSE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse"
BRON = "NDW (Nationaal Dataportaal Wegverkeer), open data"
LET_OP = (
    "Dit zijn ongecontroleerde berichten van de wegbeheerders, en niet elke beheerder meldt alles. Files staan er "
    "alleen in als ze gemeld zijn. Voor onderweg heeft de verkeersinformatie op de radio of in je navigatie de "
    "laatste stand."
)
TTL = 2 * 60
VILD = Path(__file__).parent.parent / "data" / "vild.json"
RADIUS_KM, MAX_RADIUS_KM = 8, 25
INCIDENTS = ("ongeluk", "pech", "obstakel")
STALE = timedelta(hours=24)
MAX_ITEMS = 12
S = "{http://datex2.eu/schema/3/situation}"
C = "{http://datex2.eu/schema/3/common}"
L = "{http://datex2.eu/schema/3/locationReferencing}"
XSI = "{http://www.w3.org/2001/XMLSchema-instance}type"
ROAD = re.compile(r"[ANS]\d{1,3}")
# Groups in the answer, most urgent first.
GROUPS = (
    ("files", ("file",)),
    ("ongelukken_en_pech", ("ongeluk", "pech", "obstakel")),
    ("afsluitingen", ("dicht", "brug")),
    ("werkzaamheden", ("rijstrook", "werk")),
    ("snelheid", ("snelheid",)),
)
JAMS = {
    "stationaryTraffic": "stilstaand verkeer", "queuingTraffic": "file", "slowTraffic": "langzaam rijdend verkeer",
    "heavyTraffic": "druk verkeer",
}
VEHICLES = {
    "brokenDownVehicle": "pechgeval", "vehicleOnFire": "voertuig in brand", "abandonedVehicle": "achtergelaten voertuig",
    "vehicleStuck": "vastgelopen voertuig", "slowMovingMaintenanceVehicle": "langzaam rijdend werkvoertuig",
    "slowVehicle": "langzaam rijdend voertuig", "vehicleOnWrongCarriageway": "spookrijder",
    "vehicleCarryingHazardousMaterials": "voertuig met gevaarlijke stoffen",
}
OBSTRUCTIONS = {
    "objectOnTheRoad": "voorwerp op de weg", "peopleOnRoadway": "mensen op de weg", "animalsOnTheRoad": "dieren op de weg",
    "fallenTrees": "omgevallen boom", "spillageOnTheRoad": "lading op de weg", "debris": "rommel op de weg",
}
MEASURES = {
    "roadClosed": ("dicht", "weg dicht"), "carriagewayClosures": ("dicht", "weg dicht"),
    "laneClosures": ("rijstrook", "rijstrook dicht"), "lanesDeviated": ("rijstrook", "rijstroken verlegd"),
    "narrowLanes": ("rijstrook", "smallere rijstroken"), "contraflow": ("rijstrook", "verkeer over de andere rijbaan"),
    "useOfSpecifiedLanesOrCarriagewaysAllowed": ("rijstrook", "rijden over andere rijstroken"),
    "useSpecifiedLanesOrCarriageways": ("rijstrook", "rijden over andere rijstroken"),
    "hardShoulderRunningInOperation": ("rijstrook", "spitsstrook of vluchtstrook open"),
}
CAUSES = {
    "roadMaintenance": "werkzaamheden", "constructionWork": "werkzaamheden", "publicEvent": "een evenement",
    "accident": "een ongeluk", "roadworks": "werkzaamheden",
}
SLIP_ROADS = {"exitSlipRoad": "afrit", "entrySlipRoad": "oprit", "connectingCarriageway": "verbindingsweg"}
OTHER = {
    "PoorEnvironmentConditions": "slecht zicht of zwaar weer", "WeatherRelatedRoadConditions": "gladheid of water op de weg",
    "NonWeatherRelatedRoadConditions": "slecht wegdek of olie op de weg", "PublicEvent": "evenement",
    "AuthorityOperation": "actie van politie of andere hulpdiensten", "DisturbanceActivity": "verstoring",
}


@cache
def _vild() -> tuple[str, dict[str, list[str]]]:
    try:
        data = json.loads(VILD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("Verkeer: geen VILD-tabel, dus geen wegnummers")
        return "", {}
    return data.get("versie", ""), data.get("punten", {})


@cache
def _new_table(table: str) -> None:
    """Said once per version: NDW moved to a newer VILD, so roads and exits are left out until the table is made again."""
    log.warning("Verkeer: NDW gebruikt VILD-versie %s, de tabel is %s. Draai scripts/ndw_vild.py", table, _vild()[0])


def _text(element: ET.Element | None, path: str) -> str | None:
    found = element.find(path) if element is not None else None
    return found.text.strip() if found is not None and found.text else None


def _kind(record: ET.Element, kind: str) -> tuple[str, str] | None:
    """The group and the Dutch words for a message, or None for one that is left out (like a diversion)."""
    if kind == "AbnormalTraffic":
        return "file", JAMS.get(_text(record, f"{S}abnormalTrafficType") or "", "file")
    if kind == "Accident":
        return "ongeluk", "ongeluk"
    if kind == "VehicleObstruction":
        name = VEHICLES.get(_text(record, f"{S}vehicleObstructionType") or "")
        comment = " ".join(_text(part, f".//{C}value") or "" for part in record.iter(f"{S}generalPublicComment"))
        if not name:
            name = "pijlwagen op de weg" if "pijlwagen" in comment.lower() else "stilstaand voertuig"
        return "pech", name
    if kind in ("GeneralObstruction", "AnimalPresenceObstruction", "EnvironmentalObstruction", "InfrastructureDamageObstruction"):
        return "obstakel", OBSTRUCTIONS.get(_text(record, f"{S}obstructionType") or "", "obstakel op de weg")
    if kind == "RoadOrCarriagewayOrLaneManagement":
        group, name = MEASURES.get(_text(record, f"{S}roadOrCarriagewayOrLaneManagementType") or "", ("werk", "afzetting"))
        lanes = {_text(part, f"{L}carriageway") for part in record.iter(f"{L}carriageway")}
        slip = next((SLIP_ROADS[lane] for lane in lanes if lane in SLIP_ROADS), None)
        if slip and group == "dicht":
            name = f"{slip} dicht"
        cause = CAUSES.get(_text(record, f"{S}cause/{S}causeType") or "")
        return group, f"{name} door {cause}" if cause else name
    if kind == "SpeedManagement":
        limit = _text(record, f"{S}temporarySpeedLimit")
        return ("snelheid", f"maximaal {round(float(limit))} km/u") if limit else None
    if kind == "GeneralNetworkManagement":
        if "bridge" in (_text(record, f"{S}generalNetworkManagementType") or "").lower():
            return "brug", "brug open voor schepen"
        return None
    if kind in OTHER:
        return "obstakel", OTHER[kind]
    return None


def _points(record: ET.Element) -> list[tuple[float, float]]:
    points = []
    for point in record.iter(f"{L}pointCoordinates"):
        lat, lon = _text(point, f"{L}latitude"), _text(point, f"{L}longitude")
        if lat and lon:
            points.append((float(lat), float(lon)))
    for line in record.iter(f"{L}posList"):
        numbers = [float(number) for number in (line.text or "").split()]
        points += list(zip(numbers[0::2], numbers[1::2]))
    return points


def _length(points: list[tuple[float, float]]) -> float:
    return sum(_metres(a, b) for a, b in zip(points, points[1:]))


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    dlat = math.radians(b[0] - a[0])
    dlon = math.radians(b[1] - a[1]) * math.cos(math.radians(a[0]))
    return 6_371_000 * math.hypot(dlat, dlon)


def _parse(raw: bytes) -> dict[str, Any]:
    """The messages that count now, in a simple form."""
    root = ET.fromstring(gzip.decompress(raw))
    now = now_local()
    messages = []
    for record in root.iter(f"{S}situationRecord"):
        kind = (record.get(XSI) or "").split(":")[-1]
        what = _kind(record, kind)
        if not what or _text(record, f"{S}probabilityOfOccurrence") == "riskOf":
            continue
        if _text(record, f".//{C}validityStatus") == "suspended":
            continue
        start = parse_time(_text(record, f".//{C}overallStartTime"))
        end = parse_time(_text(record, f".//{C}overallEndTime"))
        if (start and start > now) or (end and end < now):
            continue
        # A car that has stood still for days is a message nobody closed, not a car on the road.
        if what[0] in INCIDENTS and start and now - start > STALE:
            continue
        points = _points(record)
        if not points:
            continue
        primary = _text(record, f".//{L}alertCMethod4PrimaryPointLocation//{L}specificLocation") or _text(
            record, f".//{L}alertCMethod2PrimaryPointLocation//{L}specificLocation"
        ) or _text(record, f".//{L}specificLocation")
        secondary = _text(record, f".//{L}alertCMethod4SecondaryPointLocation//{L}specificLocation") or _text(
            record, f".//{L}alertCMethod2SecondaryPointLocation//{L}specificLocation"
        )
        length = _text(record, f".//{S}queueLength") or _text(record, f".//{S}lengthAffected")
        messages.append({
            "group": what[0], "wat": what[1], "start": start, "end": end, "points": points,
            "primary": primary, "secondary": secondary if secondary != primary else None,
            "direction": _text(record, f".//{L}alertCDirectionCoded"),
            "table": _text(record, f".//{L}alertCLocationTableNumber"),
            "source": _text(record, f"{S}source/{C}sourceName/{C}values/{C}value") or "",
            "length": float(length) if length else (_length(points) if what[0] == "file" else 0.0),
        })
    published = parse_time(_text(root, f".//{C}publicationTime"))
    return {"published": published, "messages": messages}


async def _picture() -> dict[str, Any]:
    async def fetch() -> dict[str, Any]:
        raw = await get_bytes(URL, source="NDW")
        return await asyncio.to_thread(_parse, raw)

    return await cached(("verkeer",), TTL, fetch)


def _road_point(code: str | None) -> list[str] | None:
    _, points = _vild()
    return points.get(code) if code else None


def _point_name(point: list[str]) -> str:
    """'afrit 4 Hulsberg' or 'kruising Edisonstraat' from a VILD point."""
    kind, first, second, exit_number = point[2].lower(), point[3], point[4], point[5]
    kind = re.sub(r"\s*\(.*\)", "", kind)
    if kind in ("afrit", "aansluiting") and exit_number:
        kind = f"{kind} {exit_number}"
    if kind == "kruising" and second and second != first:
        return f"kruising {first} en {second}"
    return f"{kind} {first}".strip()


def _where(message: dict) -> str | None:
    """Where on the road, from the VILD codes: 'A12 tussen afrit 6 en afrit 7, richting Utrecht'."""
    version, _ = _vild()
    if not message["primary"]:
        return None
    if message["table"] and version and not version.startswith(message["table"]):
        _new_table(message["table"])
        return None
    head, tail = _road_point(message["primary"]), _road_point(message["secondary"])
    if not head:
        return None
    # A city road has an internal number like H153, so it goes by its street name.
    road = head[0] if ROAD.fullmatch(head[0]) else head[1]
    place = f"tussen {_point_name(tail)} en {_point_name(head)}" if tail else f"bij {_point_name(head)}"
    toward = {"positive": head[8], "negative": head[7]}.get(message["direction"] or "")
    town = "" if ROAD.fullmatch(head[0]) or not head[6] else f", {head[6]}"
    return f"{road} {place}".strip() + town + (f", richting {toward}" if toward and toward != head[3] else "")


async def _street(point: tuple[float, float]) -> str | None:
    """The street at a point, from PDOK, for a message without a road code."""
    lat, lon = f"{point[0]:.4f}", f"{point[1]:.4f}"
    params = {"lat": lat, "lon": lon, "type": "weg", "rows": 1, "fl": "straatnaam,woonplaatsnaam"}
    try:
        data = await cached(("verkeer-straat", lat, lon), 7 * 24 * 3600, lambda: get_json(REVERSE, params=params, source="PDOK Locatieserver"))
    except ToolError:
        return None
    docs = data.get("response", {}).get("docs", [])
    if not docs or not docs[0].get("straatnaam"):
        return None
    return ", ".join(part for part in (docs[0]["straatnaam"], docs[0].get("woonplaatsnaam")) if part)


def _by(source: str) -> str | None:
    if re.match(r"(ZN|WN|NN|ON|MN|ZD|WNZ|WNN)\b|NLRWS|.*\bRWS\b", source):
        return "Rijkswaterstaat"
    if source.startswith(("Gemeente", "Provincie", "Waterschap", "Hoogheemraadschap")):
        return plaatsen.common_name(source)
    return None


def _road_key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


async def verkeer(waar: str | None = None, weg: str | None = None, straal_km: float | None = None) -> dict[str, Any]:
    road = _road_key(weg) if weg else None
    if road and not ROAD.fullmatch(road):
        raise ToolError(f"'{weg}' is geen wegnummer. Noem een snelweg of provinciale weg, zoals A12 of N44.")
    centre = None
    if waar:
        place = await plaatsen.zoek(waar)
        centre = (place.lat, place.lon)
    radius = min(max(float(straal_km or RADIUS_KM), 1.0), MAX_RADIUS_KM) * 1000
    picture = await _picture()

    chosen, seen = [], set()
    for message in picture["messages"]:
        on_road = road and any(
            (_road_point(code) or [""])[0] == road for code in (message["primary"], message["secondary"]) if code
        )
        if road and not on_road:
            continue
        distance = min(_metres(centre, point) for point in message["points"]) if centre else None
        if distance is not None and distance > radius:
            continue
        # Heel Nederland: only what matters on the main roads, not every street with works.
        main_road = ROAD.fullmatch((_road_point(message["primary"]) or [""])[0])
        if not road and not centre and not (main_road and message["group"] in ("file", "ongeluk", "dicht")):
            continue
        # Road authorities often send the same closure twice, for each side or in two systems.
        middle = message["points"][len(message["points"]) // 2]
        same = (message["wat"], message["primary"], message["secondary"], message["direction"]) if message["primary"] else (
            message["wat"], round(middle[0], 3), round(middle[1], 3)
        )
        if same in seen:
            continue
        seen.add(same)
        chosen.append((message, distance))

    order = [kind for _, kinds in GROUPS for kind in kinds]
    chosen.sort(key=lambda pair: (order.index(pair[0]["group"]), pair[1] or 0, -(pair[0]["length"] or 0)))
    shown = chosen[:MAX_ITEMS]
    streets = await asyncio.gather(*(
        _street(message["points"][len(message["points"]) // 2]) if not _where(message) else asyncio.sleep(0, None)
        for message, _ in shown
    ))
    area = " ".join(part for part in (road, f"binnen {radius / 1000:g} km van {place.naam}" if centre else "") if part)
    result: dict[str, Any] = {
        "gebied": area or "de grote wegen in heel Nederland",
        "bijgewerkt": fmt_datetime(picture["published"]),
    }
    for name, kinds in GROUPS:
        items = []
        for (message, distance), street in zip(shown, streets):
            if message["group"] not in kinds:
                continue
            item: dict[str, Any] = {"wat": message["wat"], "waar": _where(message) or street}
            if message["group"] == "file" and message["length"]:
                item["lengte_km"] = round(message["length"] / 1000, 1)
            if distance is not None:
                item["afstand_km"] = round(distance / 1000, 1)
            if message["start"]:
                item["sinds"] = fmt_datetime(message["start"])
            if message["end"]:
                item["tot"] = fmt_datetime(message["end"])
            by = _by(message["source"])
            if by:
                item["door"] = by
            items.append({key: value for key, value in item.items() if value is not None})
        if items:
            result[name] = items
    counts = {name: sum(message["group"] in kinds for message, _ in chosen) for name, kinds in GROUPS}
    result["aantal"] = {name: count for name, count in counts.items() if count}
    if len(chosen) > MAX_ITEMS:
        result["meer"] = f"Er zijn {len(chosen)} berichten; dit zijn de {MAX_ITEMS} belangrijkste. Noem een weg of een kleiner gebied."
    if not chosen:
        result["uitleg"] = "Op dit moment meldt NDW hier geen files, ongelukken, afsluitingen of werkzaamheden."
    result["let_op"] = LET_OP
    result["bron"] = BRON
    return result

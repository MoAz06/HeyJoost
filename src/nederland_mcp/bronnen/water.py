"""Live water levels and water temperature from Rijkswaterstaat WaterWebservices (ddapi20)."""

import math
from datetime import timedelta
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, fmt_datetime, get_json, normalize, now_local, parse_time, post_json

BASE = "https://ddapi20-waterwebservices.rijkswaterstaat.nl"
CATALOG_URL = f"{BASE}/METADATASERVICES/OphalenCatalogus"
LATEST_URL = f"{BASE}/ONLINEWAARNEMINGENSERVICES/OphalenLaatsteWaarnemingen"
LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
SOURCE = "Rijkswaterstaat Waterinfo"

QUANTITIES = {"waterstand": "WATHTE", "watertemperatuur": "T"}
MAX_AGE = timedelta(hours=48)
MISSING = 1e8  # Rijkswaterstaat uses 999999999 for missing values


async def _stations() -> list[dict]:
    async def fetch() -> list[dict]:
        data = await post_json(CATALOG_URL, {"CatalogusFilter": {"Compartimenten": True, "Grootheden": True}}, source=SOURCE)
        meta = {m.get("AquoMetadata_MessageID"): m for m in data.get("AquoMetadataLijst", [])}
        locations = {loc.get("Locatie_MessageID"): loc for loc in data.get("LocatieLijst", [])}
        stations: dict[str, dict] = {}
        for link in data.get("AquoMetadataLocatieLijst", []):
            m = meta.get(link.get("AquoMetaData_MessageID", link.get("AquoMetadata_MessageID")))
            loc = locations.get(link.get("Locatie_MessageID"))
            if not m or not loc or m.get("Compartiment", {}).get("Code") != "OW":
                continue
            quantity = m.get("Grootheid", {}).get("Code")
            if quantity not in QUANTITIES.values():
                continue
            station = stations.setdefault(loc["Code"], {
                "code": loc["Code"],
                "naam": loc.get("Naam") or loc["Code"],
                "lat": loc.get("Lat"),
                "lon": loc.get("Lon"),
                "grootheden": set(),
            })
            station["grootheden"].add(quantity)
        return list(stations.values())

    return await cached(("rws-catalogus",), 24 * 3600, fetch)


def _distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _name_matches(stations: list[dict], query: str) -> list[dict]:
    q = normalize(query)
    exact = [s for s in stations if q in (normalize(s["naam"]), normalize(s["code"]))]
    starts = [s for s in stations if s not in exact and normalize(s["naam"]).startswith(q)]
    contains = [s for s in stations if s not in exact and s not in starts and q in normalize(s["naam"])]
    return exact + starts + contains


async def _geocode(place: str) -> tuple[float, float] | None:
    data = await cached(
        ("geocode", place.lower()),
        24 * 3600,
        lambda: get_json(LOCATIESERVER, params={"q": place, "rows": 1, "fl": "centroide_ll"}, source="PDOK Locatieserver"),
    )
    docs = data.get("response", {}).get("docs", [])
    point = docs[0].get("centroide_ll", "") if docs else ""
    if not point.startswith("POINT("):
        return None
    lon, lat = point[6:-1].split()
    return float(lat), float(lon)


async def _latest(codes: list[str], quantity: str) -> dict[str, dict]:
    body = {
        "LocatieLijst": [{"Code": code} for code in codes],
        "AquoPlusWaarnemingMetadataLijst": [
            {"AquoMetadata": {"Compartiment": {"Code": "OW"}, "Grootheid": {"Code": quantity}}}
        ],
    }
    data = await cached(("rws-laatste", quantity, tuple(codes)), 300, lambda: post_json(LATEST_URL, body, source=SOURCE))
    now = now_local()
    best: dict[str, dict] = {}
    for series in data.get("WaarnemingenLijst", []):
        code = series.get("Locatie", {}).get("Code")
        unit = series.get("AquoMetadata", {}).get("Eenheid", {}).get("Code")
        for measurement in series.get("MetingenLijst", []):
            when = parse_time(measurement.get("Tijdstip"))
            value = measurement.get("Meetwaarde", {}).get("Waarde_Numeriek")
            if when is None or value is None or abs(value) >= MISSING or now - when > MAX_AGE:
                continue
            try:
                depth = abs(float(measurement.get("WaarnemingMetadata", {}).get("Bemonsteringshoogte") or 0))
            except ValueError:
                depth = 0.0
            rank = (when, -depth)
            if code not in best or rank > best[code]["rank"]:
                best[code] = {"rank": rank, "tijd": when, "waarde": value, "eenheid": unit}
    return best


async def _measure(place: str, quantity: str, stations: list[dict]) -> list[dict]:
    with_quantity = [s for s in stations if quantity in s["grootheden"]]
    named = _name_matches(with_quantity, place)[:15]

    anchor_station = next(iter(_name_matches(stations, place)), None)
    anchor = (anchor_station["lat"], anchor_station["lon"]) if anchor_station else await _geocode(place)
    nearby: list[dict] = []
    if anchor:
        located = [s for s in with_quantity if s["lat"] is not None and s["lon"] is not None]
        # Many points near the coast (bathing water, monitoring rounds) have no live data, so look well past the closest few.
        nearby = sorted(located, key=lambda s: _distance_km(anchor, (s["lat"], s["lon"])))[:60]

    candidates = list({s["code"]: s for s in named + nearby}.values())
    if not candidates:
        return []
    latest = await _latest([s["code"] for s in candidates], quantity)

    named_codes = {s["code"] for s in named}
    results = []
    for station in candidates:
        hit = latest.get(station["code"])
        if not hit:
            continue
        distance = _distance_km(anchor, (station["lat"], station["lon"])) if anchor and station["lat"] else None
        results.append((station["code"] not in named_codes, distance if distance is not None else 1e9, station, hit))
    results.sort(key=lambda r: (r[0], r[1]))

    output = []
    for _, distance, station, hit in results[:3]:
        row: dict[str, Any] = {"meetpunt": station["naam"], "gemeten_om": fmt_datetime(hit["tijd"])}
        if quantity == "WATHTE":
            cm = round(hit["waarde"])
            row["waterstand_cm_ten_opzichte_van_NAP"] = cm
            row["omschrijving"] = f"{abs(cm)} cm {'boven' if cm >= 0 else 'onder'} NAP"
        else:
            row["watertemperatuur_c"] = round(hit["waarde"], 1)
        if distance is not None and distance < 1e9:
            row["afstand_km"] = round(distance, 1)
        output.append(row)
    return output


async def water_actueel(plaats: str, soort: Literal["waterstand", "watertemperatuur", "beide"] = "beide") -> dict:
    stations = await _stations()
    kinds = list(QUANTITIES) if soort == "beide" else [soort]
    result: dict[str, Any] = {"plaats": plaats}
    for kind in kinds:
        rows = await _measure(plaats, QUANTITIES[kind], stations)
        result[kind] = rows or "Geen actuele meting gevonden in de buurt van deze plaats."
    if all(isinstance(result[k], str) for k in kinds):
        raise ToolError(
            f"Geen actuele metingen gevonden voor '{plaats}'. Probeer een plaats aan de kust of aan een grote rivier, "
            "zoals Hoek van Holland, IJmuiden, Vlissingen, Den Helder of Lobith."
        )
    result["bron"] = "Rijkswaterstaat Waterinfo (WaterWebservices), ongecontroleerde actuele metingen"
    return result

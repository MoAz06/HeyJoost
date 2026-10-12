"""Water from Rijkswaterstaat WaterWebservices (ddapi20): live water levels and temperature, the tide and forecasts.

The tide is computed from sun and moon (astronomisch), for years ahead. The forecast (verwachting) adds the wind
and the rivers, about two days ahead. Both are CC0.
"""

import math
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from ..common import AMSTERDAM, cached, fmt_datetime, get_json, normalize, now_local, parse_time, post_json
from . import plaats as plaatsen

BASE = "https://ddapi20-waterwebservices.rijkswaterstaat.nl"
CATALOG_URL = f"{BASE}/METADATASERVICES/OphalenCatalogus"
LATEST_URL = f"{BASE}/ONLINEWAARNEMINGENSERVICES/OphalenLaatsteWaarnemingen"
SERIES_URL = f"{BASE}/ONLINEWAARNEMINGENSERVICES/OphalenWaarnemingen"
LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
SOURCE = "Rijkswaterstaat Waterinfo"
BRON_VERWACHTING = "Rijkswaterstaat Waterinfo: berekend getij en waterstandsverwachting (CC0)"

QUANTITIES = {"waterstand": "WATHTE", "watertemperatuur": "T"}
MAX_AGE = timedelta(hours=48)
MISSING = 1e8  # Rijkswaterstaat uses 999999999 for missing values
# High and low water as computed from sun and moon, the type of each (hoogwater or laagwater), and the forecast.
SERIES = {
    "getij": {"Grootheid": {"Code": "WATHTE"}, "Groepering": {"Code": "GETETBRKD2"}, "ProcesType": "astronomisch"},
    "getijsoort": {
        "Grootheid": {"Code": "NVT"}, "Groepering": {"Code": "GETETBRKD2"}, "Typering": {"Code": "GETETTPE"},
        "ProcesType": "astronomisch",
    },
    "verwachting": {"Grootheid": {"Code": "WATHTE"}, "ProcesType": "verwachting"},
}
NEAR_KM = 20
# Towns with several stations of their own name, and the one in town.
STATION_ALIASES = {"rotterdam": "rotterdam.nieuwemaas.boerengat"}
# High or low water in the forecast is the highest or lowest point within SPAN on either side, and the table's high
# or low water belongs to it when it is within MATCH. The tide turns about every six hours, so the next high water
# is about twelve hours away.
SPAN = timedelta(hours=3)
MATCH = timedelta(hours=4)
TIDE_DAYS_AHEAD = 365
EXPLAIN = (
    "Bij volgens 'verwachting' rekent Rijkswaterstaat de wind en de rivieren mee: dat komt het dichtst bij wat er "
    "echt gebeurt, maar gaat maar twee dagen vooruit. In 'getijtabel' staat dan het berekende getij zonder wind. Bij "
    "volgens 'getijtabel' is het alleen het berekende getij; wind kan het water tientallen centimeters hoger of lager "
    "zetten."
)
FORECAST_STEP = timedelta(hours=3)
WEEKDAYS = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")


async def _stations() -> list[dict]:
    async def fetch() -> list[dict]:
        catalog = {"Compartimenten": True, "Grootheden": True, "ProcesTypes": True, "Groeperingen": True}
        data = await post_json(CATALOG_URL, {"CatalogusFilter": catalog}, source=SOURCE)
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
                "getij": False,
                "verwachting": False,
            })
            # "grootheden" holds what is measured, for water_actueel; the tide and the forecast are computed.
            process = m.get("ProcesType") or "meting"
            if process == "meting":
                station["grootheden"].add(quantity)
            elif quantity == "WATHTE" and process == "astronomisch":
                station["getij"] |= (m.get("Groepering") or {}).get("Code") == "GETETBRKD2"
            elif quantity == "WATHTE" and process == "verwachting":
                station["verwachting"] = True
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


async def _series(code: str, name: str, first: date, last: date, ttl: int) -> list[tuple[datetime, float | None, str]]:
    """One series at a location from the first to the last day: (time, number, text). Empty when there is none."""
    begin = datetime.combine(first, datetime.min.time(), AMSTERDAM)
    end = datetime.combine(last + timedelta(days=1), datetime.min.time(), AMSTERDAM)
    body = {
        "Locatie": {"Code": code},
        "AquoPlusWaarnemingMetadata": {"AquoMetadata": {"Compartiment": {"Code": "OW"}, **SERIES[name]}},
        "Periode": {
            "Begindatumtijd": begin.isoformat(timespec="milliseconds"),
            "Einddatumtijd": end.isoformat(timespec="milliseconds"),
        },
    }
    data = await cached(
        ("rws-reeks", code, name, first, last), ttl, lambda: post_json(SERIES_URL, body, source=SOURCE)
    )
    points = []
    for series in data.get("WaarnemingenLijst") or []:
        for measurement in series.get("MetingenLijst") or []:
            when = parse_time(measurement.get("Tijdstip"))
            value = measurement.get("Meetwaarde") or {}
            number = value.get("Waarde_Numeriek")
            if when is None or (number is not None and abs(number) >= MISSING):
                continue
            points.append((when, number, value.get("Waarde_Alfanumeriek") or ""))
    return sorted(points, key=lambda point: point[0])


def _own_station(place: str, stations: list[dict]) -> dict | None:
    """A station with exactly the place's name, like Scheveningen, Venlo or Europlatform."""
    q = normalize(place)
    code = STATION_ALIASES.get(q)
    return next((s for s in stations if code == s["code"] or q in (normalize(s["naam"]), normalize(s["code"]))), None)


def _near(anchor: tuple[float, float], stations: list[dict]) -> list[tuple[dict, float]]:
    """Stations within NEAR_KM, the nearest first."""
    located = [(s, _distance_km(anchor, (s["lat"], s["lon"]))) for s in stations if s["lat"] is not None and s["lon"] is not None]
    return sorted((pair for pair in located if pair[1] <= NEAR_KM), key=lambda pair: pair[1])


def _peaks(forecast: list) -> list[tuple[datetime, float, str]]:
    """High and low water in the forecast: the highest or lowest point within SPAN on either side."""
    points = [(moment, value) for moment, value, _ in forecast if value is not None]
    times = [moment for moment, _ in points]
    peaks = []
    for i, (moment, value) in enumerate(points):
        # Near the ends of the forecast it is unknown whether the water still rises or falls.
        if moment - SPAN < times[0] or moment + SPAN > times[-1]:
            continue
        low, high = bisect_left(times, moment - SPAN), bisect_right(times, moment + SPAN)
        window = [points[j][1] for j in range(low, high)]
        before = window[: i - low]
        # On a flat top or bottom, only its first point counts.
        if value == max(window) and value not in before:
            peaks.append((moment, value, "hoogwater"))
        elif value == min(window) and value not in before:
            peaks.append((moment, value, "laagwater"))
    return peaks


def _tide(extremes: list, kinds: list, forecast: list, first: date, last: date) -> list[dict]:
    """High and low water per day. Where the forecast reaches, it leads, with the tide table next to it."""
    kind_at = {when: text for when, _, text in kinds if text in ("hoogwater", "laagwater")}
    table = []
    for i, (when, cm, _) in enumerate(extremes):
        if cm is None:
            continue
        kind = kind_at.get(when)
        if not kind:
            # Without its type, an extreme is high water when it is higher than its neighbours.
            neighbours = [extremes[j][1] for j in (i - 1, i + 1) if 0 <= j < len(extremes) and extremes[j][1] is not None]
            kind = "hoogwater" if neighbours and cm > max(neighbours) else "laagwater"
        table.append((when, cm, kind))

    peaks = _peaks(forecast)
    points = [(moment, value) for moment, value, _ in forecast if value is not None]
    rows = []
    for when, cm, kind in peaks:
        row: dict[str, Any] = {"tijd": when.strftime("%H:%M"), "soort": kind, "cm_NAP": round(cm), "volgens": "verwachting"}
        same = [t for t in table if t[2] == kind and abs(t[0] - when) <= MATCH]
        if same:
            match = min(same, key=lambda t: abs(t[0] - when))
            row["getijtabel"] = f"{match[0].strftime('%H:%M')}, {round(match[1])} cm"
        rows.append((when, row))
    for when, cm, kind in table:
        if any(k == kind and abs(moment - when) <= MATCH for moment, _, k in peaks):
            continue
        row = {"tijd": when.strftime("%H:%M"), "soort": kind, "cm_NAP": round(cm), "volgens": "getijtabel"}
        if points and points[0][0] <= when <= points[-1][0]:
            if points[0][0] + SPAN <= when <= points[-1][0] - SPAN:
                # The forecast reaches here and shows no such high or low water, so it does not happen like that.
                continue
            # Near the end of the forecast its turn cannot be seen yet, but its height at the table's time can.
            nearest = min(points, key=lambda point: abs(point[0] - when))
            row = {**row, "cm_NAP": round(nearest[1]), "volgens": "verwachting", "getijtabel": f"{row['tijd']}, {round(cm)} cm"}
        rows.append((when, row))

    days: dict[date, list[dict]] = {}
    for when, row in sorted(rows, key=lambda pair: pair[0]):
        if first <= when.date() <= last:
            days.setdefault(when.date(), []).append(row)
    return [
        {"datum": day.isoformat(), "dag": WEEKDAYS[day.weekday()], "hoog_en_laagwater": rows}
        for day, rows in sorted(days.items())
    ]


def _levels(forecast: list, start: datetime, end: datetime) -> dict[str, Any]:
    """The forecast water level every few hours, plus its highest and lowest point."""
    points = [(moment, value) for moment, value, _ in forecast if value is not None and start <= moment < end]
    if not points:
        return {}
    steps, next_step = [], points[0][0]
    for moment, value in points:
        if moment >= next_step:
            steps.append({"tijd": fmt_datetime(moment), "cm_NAP": round(value)})
            next_step = moment + FORECAST_STEP
    high, low = max(points, key=lambda p: p[1]), min(points, key=lambda p: p[1])
    return {
        "verwachte_waterstand": steps,
        "hoogste": {"tijd": fmt_datetime(high[0]), "cm_NAP": round(high[1])},
        "laagste": {"tijd": fmt_datetime(low[0]), "cm_NAP": round(low[1])},
        "verwachting_tot": fmt_datetime(points[-1][0]),
    }


async def water_verwachting(plaats: str, datum: str | None = None) -> dict:
    if not plaats or not plaats.strip():
        raise ToolError("Noem een plaats aan de kust of aan een grote rivier, bijvoorbeeld Scheveningen of Venlo.")
    now = now_local()
    today = now.date()
    try:
        day = date.fromisoformat(datum.strip()) if datum else None
    except ValueError as exc:
        raise ToolError(f"Gebruik de datum als JJJJ-MM-DD, bijvoorbeeld {today.isoformat()}.") from exc
    if day is not None and not today - timedelta(days=1) <= day <= today + timedelta(days=TIDE_DAYS_AHEAD):
        raise ToolError("Eb en vloed kan ik opzoeken van gisteren tot een jaar vooruit.")
    first, last = (day, day) if day else (today, today + timedelta(days=1))

    stations = [s for s in await _stations() if s["getij"] or s["verwachting"]]
    own = _own_station(plaats, stations)
    name = (own["naam"] if normalize(own["naam"]) == normalize(plaats) else plaats.strip()) if own else None
    if not own:
        anchor, not_found = None, None
        try:
            place = await plaatsen.zoek(plaats)
            name, anchor = place.naam, (place.lat, place.lon)
        except ToolError as exc:
            not_found = exc
        # A station named after the town, like "Harlingen, Waddenzee". A town's centre point can lie far out in
        # the water (Harlingen) or in the port (Rotterdam), so its own station beats the nearest one.
        q = normalize(plaats)
        named = [s for s in stations if normalize(s["naam"].split(",")[0]) == q]
        if not named and not_found:
            # Not a town, but maybe a water or a harbour, like the Marsdiep in "Den Helder, Marsdiep".
            named = [s for s in stations if q in (normalize(part) for part in s["naam"].split(","))]
        if named:
            own = min(named, key=lambda s: (not s["getij"], _distance_km(anchor, (s["lat"], s["lon"])) if anchor else 0))
            name = name or own["naam"]
        elif not_found:
            raise not_found
    if own:
        anchor = (own["lat"], own["lon"])

    # The nearest station with a tide table; a station listed in the catalogue does not always have one.
    tides = ([own] if own and own["getij"] else []) + [s for s, _ in _near(anchor, [s for s in stations if s["getij"]])]
    station, extremes = None, []
    for candidate in list({s["code"]: s for s in tides}.values())[:3]:
        extremes = await _series(candidate["code"], "getij", first, last, 12 * 3600)
        if extremes:
            station = candidate
            break
    if not station:
        levels = ([own] if own and own["verwachting"] else []) + [s for s, _ in _near(anchor, [s for s in stations if s["verwachting"]])]
        station = levels[0] if levels else None
    if not station:
        raise ToolError(
            f"Geen getij of waterverwachting gevonden binnen {NEAR_KM} km van {name}. Dit werkt langs de kust, in "
            "Zeeland en op de Waddenzee, aan de grote rivieren en bij het IJsselmeer."
        )

    result: dict[str, Any] = {"plaats": name, "meetpunt": station["naam"]}
    distance = _distance_km(anchor, (station["lat"], station["lon"]))
    if distance >= 1:
        result["afstand_km"] = round(distance, 1)
    # The forecast starts yesterday and reaches about two days ahead, so only ask for it when the days are in reach.
    # It starts a day early, so that high and low water just after midnight can be found in it too.
    in_reach = today - timedelta(days=1) <= last and first <= today + timedelta(days=2)
    forecast = []
    if station["verwachting"] and in_reach:
        forecast = await _series(station["code"], "verwachting", max(first - timedelta(days=1), today - timedelta(days=1)), last, 1800)
    if extremes:
        kinds = await _series(station["code"], "getijsoort", first, last, 12 * 3600)
        result["getij"] = _tide(extremes, kinds, forecast, first, last)
        result["uitleg"] = EXPLAIN
    else:
        result["getij"] = f"Voor {station['naam']} is er geen getijtabel, alleen de verwachte waterstand."
        start = datetime.combine(first, datetime.min.time(), AMSTERDAM)
        levels = _levels(forecast, max(start, now - timedelta(minutes=10)), start + timedelta(days=(last - first).days + 1))
        if not levels:
            raise ToolError(
                f"Rijkswaterstaat heeft voor {station['naam']} geen verwachting voor die dag. Die gaat ongeveer twee "
                "dagen vooruit."
            )
        result.update(levels)
    result["bron"] = BRON_VERWACHTING
    return result

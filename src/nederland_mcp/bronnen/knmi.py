"""KNMI Data Platform: the rain radar for the next two hours, and the weather warnings.

Both are CC BY 4.0, so every answer names KNMI. The radar comes from the WMS API: 13 requests for a place, kept
for ten minutes (the key allows 1,000 requests an hour), and KNMI only gets the place rounded to about a
kilometre. The warnings come from the Open Data API as one file for the whole country, shared by everyone.

KNMI replaces its warnings on 2 November 2026: the new dataset draws areas on the map instead of naming
provinces, and holds only test files until then. So the new one is used as soon as it is live, and the old one
(per province, per hour) until that moment.
"""

import asyncio
import json
import os
import re
import time
import unicodedata
from collections import deque
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, fmt_datetime, get_bytes, get_json, now_local, parse_time
from . import plaats as plaatsen

SOURCE = "KNMI"
WMS = "https://api.dataplatform.knmi.nl/wms/adaguc-server"
OPEN_DATA = "https://api.dataplatform.knmi.nl/open-data/v1/datasets"
RADAR = "radar_forecast_2.0"
RUN = re.compile(r'<Dimension name="reference_time"[^>]*default="([^"]+)"')
BRON_REGEN = "KNMI neerslagradar, verwachting voor de komende 2 uur (CC BY 4.0)"
BRON_WAARSCHUWINGEN = "KNMI weerwaarschuwingen (CC BY 4.0)"

# Less than this, in millimetres per hour, is radar noise rather than rain.
WET = 0.1
# A radar run older than this means the feed is stuck.
RUN_MAX_AGE = timedelta(minutes=30)
# The radar looks two hours ahead; every ten minutes is precise enough and keeps it at 13 requests a place.
STEP, STEPS = timedelta(minutes=10), 13
# The key allows 20 requests a second, for all visitors together; this stays at half of that.
PER_SECOND = 10

NEW = "KNMI-public-weather-warnings/versions/1.0"
OLD = "waarschuwingen_nederland_48h/versions/1.0"
# The new file comes at every change and at least once a day; the old one came at least every 6 hours.
NEW_MAX_AGE, OLD_MAX_AGE = timedelta(hours=26), timedelta(hours=12)
COLORS = {"YELLOW": "geel", "ORANGE": "oranje", "RED": "rood", "yellow": "geel", "orange": "oranje", "red": "rood"}
HEADLINE = re.compile(r"^\s*code\s+\w+\s*:\s*", re.I)
OLD_KINDS = {
    "FX": "windstoten", "IC": "gladheid of sneeuw", "LT": "onweersbuien", "RR": "zware regen",
    "VV": "mist", "WS": "waterhozen",
}
NEW_KINDS = {
    "wind": "windstoten", "rain": "zware regen", "fog": "mist", "thunderstorm": "onweersbuien",
    "thunderstorms": "onweersbuien", "snow": "sneeuw", "ice": "gladheid", "roadice": "gladheid",
    "iciness": "gladheid", "hightemperature": "hitte", "heat": "hitte", "lowtemperature": "kou", "cold": "kou",
    "waterspout": "waterhozen", "waterspouts": "waterhozen",
}
# The regions of the old warnings, by their province name at PDOK. The islands have their own region.
REGIONS = {
    "groningen": "GR", "friesland": "FR", "fryslan": "FR", "drenthe": "DR", "noordholland": "NH",
    "flevoland": "FL", "overijssel": "OV", "gelderland": "GL", "utrecht": "UT", "zuidholland": "ZH",
    "zeeland": "ZE", "noordbrabant": "NB", "brabant": "NB", "limburg": "LB", "waddeneilanden": "WAE",
    "waddenzee": "WAD", "ijsselmeer": "IJG", "ijsselmeergebied": "IJG",
}
ISLANDS = {"texel", "vlieland", "terschelling", "ameland", "schiermonnikoog"}
# Also a city: asked plainly, these mean the city, and the province needs the word "provincie".
CITIES = {"utrecht", "groningen"}


def _key(name: str, what: str) -> dict:
    key = os.environ.get(name)
    if not key:
        raise ToolError(f"{what} is nog niet beschikbaar: de server heeft geen KNMI API-key ({name}).")
    return {"Authorization": key}


def _plain(text: str) -> str:
    """Lowercase letters only, so Fryslân matches Friesland's spelling at PDOK and Noord-Holland matches noordholland."""
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower())


# The rain radar


async def _run() -> str:
    """The latest radar run, as KNMI names it (UTC). There is a new one every five minutes."""

    async def fetch() -> str:
        params = {"DATASET": RADAR, "SERVICE": "WMS", "REQUEST": "GetCapabilities"}
        body = await get_bytes(WMS, params=params, headers=_key("KNMI_WMS_API_KEY", "De regenradar"), source=SOURCE)
        match = RUN.search(body.decode("utf-8", "replace"))
        if not match:
            raise ToolError("De regenradar van het KNMI gaf geen tijd van het laatste radarbeeld. Probeer het zo nog eens.")
        return match[1]

    return await cached(("knmi-radar-run",), 120, fetch)


_sent: deque[float] = deque()


async def _turn() -> None:
    """Waits until one more radar request fits in the last second."""
    while True:
        now = time.monotonic()
        while _sent and now - _sent[0] >= 1:
            _sent.popleft()
        if len(_sent) < PER_SECOND:
            _sent.append(now)
            return
        await asyncio.sleep(1 - (now - _sent[0]))


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _rain(lat: float, lon: float, run: str, moment: datetime, headers: dict) -> float | None:
    """Rain in millimetres per hour at one point and one moment of a run; None where the radar has no value."""
    params = {
        "DATASET": RADAR, "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetFeatureInfo",
        "LAYERS": "precipitation_nowcast", "QUERY_LAYERS": "precipitation_nowcast", "SRS": "EPSG:4326",
        "BBOX": f"{lon - 0.01:.2f},{lat - 0.01:.2f},{lon + 0.01:.2f},{lat + 0.01:.2f}", "WIDTH": 2, "HEIGHT": 2,
        "X": 1, "Y": 1, "INFO_FORMAT": "application/json", "TIME": _utc(moment), "DIM_REFERENCE_TIME": run,
    }
    await _turn()
    data = await get_json(WMS, params=params, headers=headers, source=SOURCE)
    layer = data[0] if isinstance(data, list) and data else {}
    series = layer.get("data") if isinstance(layer, dict) else None
    values = next(iter(series.values()), {}) if isinstance(series, dict) and series else {}
    try:
        mm = float(next(iter(values.values())))
    except (AttributeError, StopIteration, TypeError, ValueError):
        return None
    return mm if mm >= 0 else None


async def _forecast(lat: float, lon: float, run: str, moments: list[datetime], headers: dict) -> list[float | None]:
    # Asked for several moments at once, KNMI's server answers "nodata" after the first one (seen on 13 October
    # 2026), so every moment is a request of its own.
    return list(await asyncio.gather(*(_rain(lat, lon, run, moment, headers) for moment in moments)))


def _strength(mm: float) -> str:
    return "lichte" if mm < 2.5 else "matige" if mm < 10 else "zware"


def _showers(steps: list[tuple[datetime, float | None]]) -> list[tuple[int, int]]:
    """Where it rains, as [start, end) positions in the steps."""
    showers, start = [], None
    for i, (_, mm) in enumerate(steps):
        wet = mm is not None and mm >= WET
        if wet and start is None:
            start = i
        elif not wet and start is not None:
            showers.append((start, i))
            start = None
    if start is not None:
        showers.append((start, len(steps)))
    return showers


def _summary(steps: list[tuple[datetime, float | None]], showers: list[tuple[int, int]]) -> str:
    last = steps[-1][0]
    if not showers:
        return f"Droog tot minstens {last:%H:%M}."
    start, end = showers[0]
    rain = f"{_strength(max(steps[i][1] for i in range(start, end)))} regen"
    stops = f"tot ongeveer {steps[end][0]:%H:%M}" if end < len(steps) else f"tot minstens {last:%H:%M}"
    if start == 0:
        text = f"Het regent nu, {rain}, {stops}."
    else:
        text = f"Droog tot {steps[start][0]:%H:%M}, dan {rain} {stops}."
    if len(showers) > 1:
        text += f" Daarna nog een bui rond {steps[showers[1][0]][0]:%H:%M}."
    return text


async def regen(plaats: str) -> dict:
    if not plaats or not plaats.strip():
        raise ToolError("Noem een plaats, bijvoorbeeld Leiden of Scheveningen.")
    headers = _key("KNMI_WMS_API_KEY", "De regenradar")
    place = await plaatsen.zoek(plaats)
    # About a kilometre, the size of a radar pixel, and the same point for the whole neighbourhood.
    lat, lon = round(place.lat, 2), round(place.lon, 2)
    run = await _run()
    start = parse_time(run)
    now = now_local()
    if not start or now - start > RUN_MAX_AGE:
        seen = f" Het laatste radarbeeld is van {start:%H:%M}." if start else ""
        raise ToolError(f"De regenradar van het KNMI is nu niet actueel.{seen} Het weer per uur staat bij Weer.")
    moments = [start + STEP * i for i in range(STEPS)]
    values = await cached(
        ("knmi-regen", lat, lon, run), 600, lambda: _forecast(lat, lon, run, moments, headers)
    )
    # The first image of a run is a few minutes old already; it still says what falls now.
    steps = [(moment, mm) for moment, mm in zip(moments, values) if moment + STEP > now]
    if not steps or all(mm is None for _, mm in steps):
        raise ToolError(f"De regenradar van het KNMI heeft nu geen beeld voor {place.naam}. Probeer het zo nog eens.")

    showers = _showers(steps)
    result: dict = {
        "plaats": place.naam,
        "radarbeeld_van": f"{start:%H:%M}",
        "samenvatting": _summary(steps, showers),
        "max_mm_per_uur": round(max(mm or 0 for _, mm in steps), 1),
    }
    if showers:
        result["per_10_minuten"] = [{"tijd": f"{moment:%H:%M}", "mm_per_uur": round(mm or 0, 1)} for moment, mm in steps]
    result["bron"] = BRON_REGEN
    return result


# The warnings


@dataclass
class _Warning:
    kleur: str
    soort: str
    gebied: str
    van: datetime
    tot: datetime
    tekst: str
    kop: str = ""
    gevolgen: str = ""
    advies: str = ""
    kans: str = ""
    # Potentially dangerous weather: KNMI's heads-up, before there is a warning.
    vooruit: bool = False
    # The old warnings name regions; the new ones draw areas as rings of (lon, lat).
    regios: frozenset[str] = frozenset()
    vlakken: tuple = ()

    def geldt_voor(self, place: plaatsen.Plaats | None, region: str | None) -> bool:
        if region:
            if self.regios:
                return region in self.regios
            # The new areas are drawn on the map, so a province is matched on the area's description.
            names = [name for name, code in REGIONS.items() if code == region] + ["nederland"]
            return any(name in _plain(self.gebied) for name in names)
        if place is None:
            return True
        if self.regios:
            return _region(place) in self.regios
        return any(_inside(place.lon, place.lat, ring) for ring in self.vlakken)

    def uit(self) -> dict:
        out = {
            "code": self.kleur,
            "soort": self.soort,
            "gebied": self.gebied,
            "van": fmt_datetime(self.van),
            "tot": fmt_datetime(self.tot),
        }
        if self.vooruit:
            out["let_op"] = "Mogelijk gevaarlijk weer: een vooraankondiging, nog geen waarschuwing."
        for name in ("kop", "tekst", "gevolgen", "advies"):
            if getattr(self, name):
                out[name] = getattr(self, name)
        if self.kans:
            out["kans"] = f"{self.kans}%"
        return out

    def kort(self) -> str:
        what = "mogelijk gevaarlijk weer" if self.vooruit else f"code {self.kleur}"
        return f"KNMI: {what} voor {self.soort} in {self.gebied}, van {fmt_datetime(self.van)} tot {fmt_datetime(self.tot)}."


def _region(place: plaatsen.Plaats) -> str | None:
    if _plain(place.gemeente) in ISLANDS:
        return "WAE"
    return REGIONS.get(_plain(place.provincie))


def _inside(lon: float, lat: float, ring: list) -> bool:
    """Whether a point lies inside a ring of [lon, lat] points (ray casting)."""
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _old(xml: bytes) -> tuple[list[_Warning], datetime | None]:
    """The old warnings: a colour per region, per phenomenon and per hour for 48 hours."""
    root = ET.fromstring(xml)
    issued = parse_time(root.findtext("metadata/report_info/report_dtg_issued"))
    names = {
        location.findtext("location_id"): location.findtext("location_descr/text/text_header")
        for location in root.iterfind("metadata/report_structure/report_locations/report_location")
    }
    # Per phenomenon and region: the coloured hours, merged into runs of the same colour and text.
    runs: dict[tuple, list[list]] = {}
    for timeslice in root.iterfind("data/cube/timeslice"):
        hour = parse_time(timeslice.findtext("timeslice_id"))
        if not hour:
            continue
        for phenomenon in timeslice.iterfind("phenomenon"):
            kind = phenomenon.findtext("phenomenon_id") or ""
            for location in phenomenon.iterfind("location"):
                colour = COLORS.get(location.findtext("color_id") or "")
                if not colour:
                    continue
                criterion = location.findtext("criterion_id") or ""
                text = next(
                    (t.findtext("text_data") or "" for t in location.iterfind("text") if t.findtext("text_language_id") == "NL"),
                    "",
                ).strip()
                state = (colour, criterion, text)
                own = runs.setdefault((kind, location.findtext("location_id")), [])
                if own and own[-1][1] == hour and own[-1][2] == state:
                    own[-1][1] = hour + timedelta(hours=1)
                else:
                    own.append([hour, hour + timedelta(hours=1), state])
    # The same warning for several regions becomes one, with all the regions.
    grouped: dict[tuple, list[str]] = {}
    for (kind, region), own in runs.items():
        for start, end, state in own:
            grouped.setdefault((kind, start, end, state), []).append(region)
    warnings = []
    for (kind, start, end, (colour, criterion, text)), regions in grouped.items():
        soort = OLD_KINDS.get(kind) or ("kou" if criterion in ("TX005", "TX006", "TX007") else "hitte" if kind == "TX" else kind)
        gebied = ", ".join(names.get(code) or code for code in regions)
        warnings.append(_Warning(colour, soort, gebied, start, end, text, regios=frozenset(regions)))
    return sorted(warnings, key=lambda w: (w.van, w.soort)), issued


def _new(snapshot: dict) -> tuple[list[_Warning], datetime | None]:
    """The new warnings: per phenomenon a set of parts, each with its own area on the map."""
    warnings = []
    for group in snapshot.get("warnings") or []:
        if group.get("operational_status") != "Actual" or group.get("status") != "active":
            continue
        texts = (group.get("texts") or {}).get("nl") or {}
        soort = NEW_KINDS.get(_plain(group.get("phenomenon") or "")) or (group.get("phenomenon") or "weer")
        for part in group.get("parts") or []:
            colour = COLORS.get(part.get("color") or "")
            start, end = parse_time(part.get("onset")), parse_time(part.get("expires"))
            if part.get("status") != "active" or not colour or not start or not end:
                continue
            area = part.get("area") or {}
            polygons = [area.get("coordinates") or []] if area.get("type") == "Polygon" else area.get("coordinates") or []
            guidance = (part.get("guidance") or {}).get("nl") or {}
            warnings.append(_Warning(
                colour, soort, ((part.get("area_description") or {}).get("nl") or "Nederland").strip(), start, end,
                # The headline names the highest colour of the set, like "Code oranje: zware storm", so it loses
                # that part: each part has its own colour.
                (texts.get("description") or "").strip(), kop=HEADLINE.sub("", texts.get("headline") or "").strip(),
                gevolgen=(guidance.get("impact") or "").strip(), advies=(guidance.get("instruction") or "").strip(),
                kans=part.get("probability_percentage_range") or "",
                vooruit=group.get("warning_type") == "potentially-dangerous-weather",
                vlakken=tuple(polygon[0] for polygon in polygons if polygon),
            ))
    return sorted(warnings, key=lambda w: (w.van, w.soort)), parse_time(snapshot.get("metadata", {}).get("generated_at"))


async def _newest(dataset: str, suffix: str, headers: dict) -> str | None:
    params = {"maxKeys": 4, "orderBy": "filename", "sorting": "desc"}
    data = await get_json(f"{OPEN_DATA}/{dataset}/files", params=params, headers=headers, source=SOURCE)
    return next((f["filename"] for f in data.get("files") or [] if f.get("filename", "").endswith(suffix)), None)


async def _file(dataset: str, filename: str, headers: dict, read) -> Any:
    """A published file, read once: KNMI never changes a file under the same name."""

    async def fetch() -> Any:
        link = await get_json(f"{OPEN_DATA}/{dataset}/files/{filename}/url", headers=headers, source=SOURCE)
        # A temporary link to the file itself, which needs no key.
        return read(await get_bytes(link["temporaryDownloadUrl"], source=SOURCE))

    return await cached(("knmi-bestand", dataset, filename), 6 * 3600, fetch)


async def _fetch() -> tuple[list[_Warning], datetime]:
    headers = _key("KNMI_OPEN_DATA_API_KEY", "Weerwaarschuwingen")
    now = now_local()
    name = await _newest(NEW, ".json", headers)
    if name:
        snapshot = await _file(NEW, name, headers, json.loads)
        # Until the switch the new dataset holds only test files, and those must never look like real warnings.
        if (snapshot.get("metadata") or {}).get("publication_type") in ("manual", "heartbeat"):
            warnings, issued = _new(snapshot)
            if issued and now - issued < NEW_MAX_AGE:
                return warnings, issued
    name = await _newest(OLD, ".xml", headers)
    if name:
        warnings, issued = await _file(OLD, name, headers, _old)
        if issued and now - issued < OLD_MAX_AGE:
            return warnings, issued
    raise ToolError("De weerwaarschuwingen van het KNMI zijn nu niet op te halen. Kijk op knmi.nl.")


async def _current() -> tuple[list[_Warning], datetime]:
    """All warnings that have not ended yet, for the whole country. A failure is kept as long as an answer, so a
    slow KNMI does not hold up every weather question."""

    async def fetch() -> tuple | str:
        try:
            return await _fetch()
        except ToolError as exc:
            return str(exc)

    state = await cached(("knmi-waarschuwingen",), 300, fetch)
    if isinstance(state, str):
        raise ToolError(state)
    warnings, issued = state
    now = now_local()
    return [w for w in warnings if w.tot > now], issued


def _asked_region(plaats: str) -> str | None:
    """A province or KNMI region asked by name, like Zeeland or de Waddeneilanden, rather than a town."""
    name = _plain(re.sub(r"^(de|het|provincie)\s+", "", plaats.strip(), flags=re.I))
    if name in CITIES and not re.match(r"provincie\s", plaats.strip(), re.I):
        return None
    return REGIONS.get(name)


async def waarschuwingen(plaats: str | None = None) -> dict:
    warnings, issued = await _current()
    result: dict = {}
    if plaats and plaats.strip():
        region = _asked_region(plaats)
        place = None if region else await plaatsen.zoek(plaats)
        name = plaats.strip() if region else place.naam
        applies = [w.geldt_voor(place, region) for w in warnings]
        here = [w for w, yes in zip(warnings, applies) if yes]
        result["plaats"] = name
        result["waarschuwingen"] = [w.uit() for w in here] or f"Geen weerwaarschuwingen van het KNMI voor {name}."
        elsewhere = [w.kort() for w, yes in zip(warnings, applies) if not yes]
        if elsewhere:
            result["elders_in_nederland"] = elsewhere
    else:
        result["waarschuwingen"] = [w.uit() for w in warnings] or "Er zijn nu geen weerwaarschuwingen van het KNMI."
    result["uitgegeven"] = fmt_datetime(issued)
    result["bron"] = BRON_WAARSCHUWINGEN
    return result


async def voor_plaats(place: plaatsen.Plaats, day: date | None = None) -> list[str]:
    """The warnings for a place in a few words, for the weather. Empty when there are none or KNMI fails."""
    try:
        warnings, _ = await _current()
    except ToolError:
        return []
    return [
        w.kort() for w in warnings
        if w.geldt_voor(place, None) and (day is None or w.van.date() <= day <= (w.tot - timedelta(seconds=1)).date())
    ]

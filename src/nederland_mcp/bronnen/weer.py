"""Weather from the models of KNMI (HARMONIE-AROME, 2.5 days) and ECMWF (after that), through Open-Meteo.

Open-Meteo is free for non-commercial use (under 10,000 calls a day). Its data is CC BY 4.0, so every answer
names Open-Meteo and the website links to it. A place is looked up at PDOK Locatieserver, and Open-Meteo only
gets that point rounded to about a kilometre.
"""

import re
import unicodedata
from datetime import date, timedelta

from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, get_json, now_local, parse_time

BASE = "https://api.open-meteo.com/v1/forecast"
LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
SOURCE = "Open-Meteo"
BRON = "KNMI en ECMWF via Open-Meteo.com (CC BY 4.0)"
DAYS_AHEAD = 6

# Towns, municipalities and districts, and a postcode for its town. Never a street or an address: a street name
# without its town is in many places, and the weather needs no more than the neighbourhood.
AREAS = "type:(woonplaats OR gemeente OR wijk OR buurt)"
POSTCODES = "type:postcode"
FIELDS = "type,woonplaatsnaam,gemeentenaam,wijknaam,buurtnaam,centroide_ll"
OWN_NAME = {
    "woonplaats": "woonplaatsnaam", "gemeente": "gemeentenaam", "wijk": "wijknaam", "buurt": "buurtnaam",
    "postcode": "woonplaatsnaam",
}
POSTCODE = re.compile(r"\d{4}\s*[A-Za-z]{0,2}")
# PDOK ranks other places "aan Zee" above Katwijk for "Katwijk aan Zee", so that gets a second try as "Katwijk".
AAN = re.compile(r"\s+aan\s+(zee|den?\s+\S+)$", re.I)
GENERIC = {"station", "centraal", "centrum", "noord", "oost", "zuid", "west", "dorp", "stad", "haven", "strand", "buitengebied"}
# The names people use, rather than the official ones.
ALIASES = ((re.compile(r"'?s[- ]?gravenhage", re.I), "Den Haag"), (re.compile(r"'?s[- ]?hertogenbosch", re.I), "Den Bosch"))

CURRENT = "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m"
HOURLY = "temperature_2m,precipitation,weather_code,wind_speed_10m,wind_gusts_10m"
DAILY = (
    "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_hours,"
    "wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant,sunrise,sunset"
)

# WMO weather codes, as Open-Meteo gives them.
WEATHER = {
    0: "onbewolkt", 1: "vrijwel onbewolkt", 2: "half bewolkt", 3: "bewolkt",
    45: "mist", 48: "mist met rijp",
    51: "lichte motregen", 53: "motregen", 55: "dichte motregen",
    56: "lichte ijzel", 57: "ijzel",
    61: "lichte regen", 63: "regen", 65: "zware regen",
    66: "lichte ijzel", 67: "ijzel",
    71: "lichte sneeuw", 73: "sneeuw", 75: "zware sneeuw", 77: "sneeuwkorrels",
    80: "lichte buien", 81: "buien", 82: "zware buien",
    85: "lichte sneeuwbuien", 86: "sneeuwbuien",
    95: "onweer", 96: "onweer met hagel", 99: "zwaar onweer met hagel",
}
# Lower bounds of Beaufort 1 to 12 in km/h.
BEAUFORT = (1, 6, 12, 20, 29, 39, 50, 62, 75, 89, 103, 118)
DIRECTIONS = ("noord", "noordoost", "oost", "zuidoost", "zuid", "zuidwest", "west", "noordwest")
WEEKDAYS = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")


def _bft(kmh: float | None) -> int | None:
    return None if kmh is None else sum(kmh >= bound for bound in BEAUFORT)


def _wind(kmh: float | None, degrees: float | None) -> str | None:
    if kmh is None:
        return None
    if degrees is None:
        return f"{_bft(kmh)} Bft"
    return f"{DIRECTIONS[round(degrees / 45) % 8]} {_bft(kmh)} Bft"


def _round(value: float | None) -> int | None:
    return None if value is None else round(value)


def _clock(value: str | None) -> str | None:
    moment = parse_time(value)
    return moment.strftime("%H:%M") if moment else None


def _common(name: str) -> str:
    for pattern, alias in ALIASES:
        name = pattern.sub(alias, name)
    return name


def _words(text: str) -> set[str]:
    text = unicodedata.normalize("NFKD", _common(text)).encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z0-9]+", text))


def _own_name(doc: dict) -> str:
    # Districts are called like "Wijk 07 Scheveningen".
    return re.sub(r"^Wijk \d+\s+", "", doc.get(OWN_NAME.get(doc.get("type"), ""), "") or "")


def _telling(doc: dict) -> set[str]:
    """The words of a hit's own name that tell it apart: a district called Station or Zuid is in many towns."""
    return {word for word in _words(_own_name(doc)) if len(word) >= 3 and word not in GENERIC}


def _pick(query: str, docs: list[dict]) -> dict | None:
    """The hit whose own name is in the question and that matches most of it, so "Station Leiden" is Leiden."""
    asked = _words(query)
    best, best_score = None, 0
    for doc in docs:
        telling = _telling(doc)
        if telling and telling <= asked:
            names = _words(_own_name(doc)) | _words(doc.get("woonplaatsnaam") or "") | _words(doc.get("gemeentenaam") or "")
            if len(asked & names) > best_score:
                best, best_score = doc, len(asked & names)
    return best


def _name(doc: dict) -> str:
    own = _common(_own_name(doc))
    town = _common(doc.get("woonplaatsnaam") or doc.get("gemeentenaam") or "")
    if doc.get("type") == "postcode":
        return town
    if doc.get("type") in ("wijk", "buurt") and town and town not in own:
        return f"{own}, {town}"
    return own


async def _search(query: str, types: str) -> list[dict]:
    params = {"q": query, "fq": types, "rows": 10, "fl": FIELDS}
    data = await cached(
        ("weer-plaats", query.lower(), types), 24 * 3600,
        lambda: get_json(LOCATIESERVER, params=params, source="PDOK Locatieserver"),
    )
    return [doc for doc in data.get("response", {}).get("docs", []) if doc.get("centroide_ll", "").startswith("POINT(")]


async def _place(plaats: str) -> tuple[str, float, float]:
    """Name and point of a place, at PDOK Locatieserver."""
    if POSTCODE.fullmatch(plaats):
        docs = await _search(plaats, POSTCODES)
        doc = docs[0] if docs else None
    else:
        doc = _pick(plaats, await _search(plaats, AREAS))
        if not doc and AAN.search(plaats):
            shorter = AAN.sub("", plaats)
            doc = _pick(shorter, await _search(shorter, AREAS))
    if not doc:
        raise ToolError(f"Geen plaats gevonden die lijkt op '{plaats}'. Noem een plaats of wijk, bijvoorbeeld Leiden of Scheveningen.")
    lon, lat = doc["centroide_ll"][6:-1].split()
    return _name(doc), float(lat), float(lon)


def _hour(series: dict, i: int) -> dict:
    return {
        "tijd": series["time"][i].replace("T", " "),
        "temperatuur": series["temperature_2m"][i],
        "neerslag_mm": series["precipitation"][i],
        "weer": WEATHER.get(series["weather_code"][i]),
        "wind_bft": _bft(series["wind_speed_10m"][i]),
        "windstoten_kmh": _round(series["wind_gusts_10m"][i]),
    }


def _day(series: dict, i: int) -> dict:
    day = date.fromisoformat(series["time"][i])
    return {
        "datum": day.isoformat(),
        "dag": WEEKDAYS[day.weekday()],
        "weer": WEATHER.get(series["weather_code"][i]),
        "min": series["temperature_2m_min"][i],
        "max": series["temperature_2m_max"][i],
        "neerslag_mm": series["precipitation_sum"][i],
        "uren_met_neerslag": _round(series["precipitation_hours"][i]),
        "wind": _wind(series["wind_speed_10m_max"][i], series["wind_direction_10m_dominant"][i]),
        "windstoten_kmh": _round(series["wind_gusts_10m_max"][i]),
        "zon_op": _clock(series["sunrise"][i]),
        "zon_onder": _clock(series["sunset"][i]),
    }


async def weer(plaats: str, datum: str | None = None) -> dict:
    if not plaats or not plaats.strip():
        raise ToolError("Noem een plaats, bijvoorbeeld Leiden of Scheveningen.")
    now = now_local()
    try:
        day = date.fromisoformat(datum.strip()) if datum else None
    except ValueError as exc:
        raise ToolError(f"Gebruik de datum als JJJJ-MM-DD, bijvoorbeeld {now.date().isoformat()}.") from exc
    last = now.date() + timedelta(days=DAYS_AHEAD)
    if day is not None and not now.date() <= day <= last:
        raise ToolError(f"Het weer kan ik opzoeken van vandaag tot en met {last.isoformat()}.")

    name, lat, lon = await _place(plaats.strip())
    # About a kilometre: close enough for the weather, and the same point for everyone in the neighbourhood.
    lat, lon = round(lat, 2), round(lon, 2)
    params = {
        "latitude": lat, "longitude": lon, "models": "knmi_seamless", "timezone": "Europe/Amsterdam",
        "current": CURRENT, "hourly": HOURLY, "daily": DAILY, "forecast_days": DAYS_AHEAD + 1,
    }
    # The models run every hour, so a quarter of an hour is fresh enough.
    data = await cached(("weer", lat, lon), 15 * 60, lambda: get_json(BASE, params=params, source=SOURCE))
    hourly, daily = data.get("hourly") or {}, data.get("daily") or {}
    if not hourly.get("time") or not daily.get("time"):
        raise ToolError("Open-Meteo gaf geen weersverwachting terug. Probeer het zo nog eens.")

    this_hour = now.strftime("%Y-%m-%dT%H:00")
    if day is None:
        start = next((i for i, t in enumerate(hourly["time"]) if t >= this_hour), 0)
        hours = range(start, min(start + 24, len(hourly["time"])))
        days = range(len(daily["time"]))
    else:
        hours = [i for i, t in enumerate(hourly["time"]) if t.startswith(day.isoformat()) and t >= this_hour]
        days = [i for i, t in enumerate(daily["time"]) if t == day.isoformat()]

    result: dict = {"plaats": name}
    current = data.get("current") or {}
    if current and (day is None or day == now.date()):
        result["nu"] = {
            "tijd": _clock(current.get("time")),
            "temperatuur": current.get("temperature_2m"),
            "gevoelstemperatuur": current.get("apparent_temperature"),
            "weer": WEATHER.get(current.get("weather_code")),
            "neerslag_mm": current.get("precipitation"),
            "wind": _wind(current.get("wind_speed_10m"), current.get("wind_direction_10m")),
            "windstoten_kmh": _round(current.get("wind_gusts_10m")),
        }
    result["uren" if day else "komende_uren"] = [_hour(hourly, i) for i in hours]
    result["dagen"] = [_day(daily, i) for i in days]
    result["bron"] = BRON
    return result

"""Weather from the models of KNMI (HARMONIE-AROME, 2.5 days) and ECMWF (after that), through Open-Meteo.

Open-Meteo is free for non-commercial use (under 10,000 calls a day). Its data is CC BY 4.0, so every answer
names Open-Meteo and the website links to it. A place is looked up at PDOK Locatieserver, and Open-Meteo only
gets that point rounded to about a kilometre. KNMI's warnings for the place come along in let_op (see knmi.py).
"""

import asyncio
from datetime import date, timedelta

from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, get_json, now_local, parse_time
from . import knmi, plaats as plaatsen

BASE = "https://api.open-meteo.com/v1/forecast"
SOURCE = "Open-Meteo"
BRON = "KNMI en ECMWF via Open-Meteo.com (CC BY 4.0)"
DAYS_AHEAD = 6

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

    place = await plaatsen.zoek(plaats)
    # About a kilometre: close enough for the weather, and the same point for everyone in the neighbourhood.
    lat, lon = round(place.lat, 2), round(place.lon, 2)
    params = {
        "latitude": lat, "longitude": lon, "models": "knmi_seamless", "timezone": "Europe/Amsterdam",
        "current": CURRENT, "hourly": HOURLY, "daily": DAILY, "forecast_days": DAYS_AHEAD + 1,
    }
    # The models run every hour, so a quarter of an hour is fresh enough.
    data, warnings = await asyncio.gather(
        cached(("weer", lat, lon), 15 * 60, lambda: get_json(BASE, params=params, source=SOURCE)),
        knmi.voor_plaats(place, day),
    )
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

    result: dict = {"plaats": place.naam}
    if warnings:
        result["let_op"] = warnings
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
    result["bron"] = BRON + (". De waarschuwingen in let_op komen van het KNMI." if warnings else "")
    return result

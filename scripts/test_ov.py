"""Offline checks of how ov_vertrek finds the stops to leave from and to go to, on a made-up piece of Rotterdam
around the Erasmus MC: no network needed.

Run from the project folder: uv run python scripts/test_ov.py
"""

import asyncio
import math
import sys
from datetime import UTC, datetime
from typing import Any

from nederland_mcp.bronnen import ns, ov
from nederland_mcp.common import AMSTERDAM

DAY = "2026-10-27"
failed: list[str] = []
asked: list[tuple[str, dict]] = []


def check(name: str, ok: bool, got: Any = None) -> None:
    print(f"{'ok  ' if ok else 'FOUT'} {name}" + ("" if ok else f": {got}"))
    if not ok:
        failed.append(name)


def spot(north: float, east: float) -> tuple[float, float]:
    """A point this many metres from the main entrance of the Erasmus MC."""
    lat = 51.911926 + north / 111_320
    return lat, 4.468644 + east / (111_320 * math.cos(math.radians(lat)))


STOPS = {
    "cs": ("Rotterdam Centraal", spot(1390, 50), ["REGIONAL_RAIL", "SUBWAY", "TRAM", "BUS"]),
    "emc": ("Rotterdam, Erasmus MC Hoofdingang", spot(0, 0), ["BUS"]),
    "dijkzigt": ("Rotterdam, Dijkzigt", spot(-100, -85), ["SUBWAY", "BUS"]),
    "breitner": ("Rotterdam, Breitnerstraat", spot(0, -296), ["BUS"]),
    "kievit": ("Rotterdam, Kievitslaan", spot(-318, 0), ["TRAM"]),
    "eendracht": ("Rotterdam, Eendrachtsplein", spot(500, 400), ["TRAM"]),
    "euromast": ("Rotterdam, Euromast", spot(-400, -110), ["TRAM"]),
    "beurs": ("Rotterdam, Beurs", spot(900, 900), ["SUBWAY", "TRAM"]),
}
# Trip, kind, line, headsign and its stops with the time there. Nothing called Erasmus MC is on the way from
# Rotterdam Centraal, as in the real timetable.
TRIPS = [
    ("t18-0839", "TRAM", "18", "Spangen", [("cs", "08:39"), ("eendracht", "08:42"), ("kievit", "08:46"), ("euromast", "08:47")]),
    ("b44-0835", "BUS", "44", "Zuidplein", [("cs", "08:35"), ("dijkzigt", "08:41"), ("beurs", "08:50")]),
    ("me-0832", "SUBWAY", "E", "Slinge", [("cs", "08:32"), ("beurs", "08:35")]),
    ("b44-1707", "BUS", "44", "Rotterdam Centraal", [("dijkzigt", "17:07"), ("cs", "17:14")]),
    ("t18-1709", "TRAM", "18", "Rotterdam Centraal", [("kievit", "17:09"), ("eendracht", "17:12"), ("cs", "17:16")]),
    ("b32-1700", "BUS", "32", "Station Zuid", [("breitner", "17:00"), ("emc", "17:01"), ("dijkzigt", "17:02"), ("beurs", "17:10")]),
    ("ma-1703", "SUBWAY", "A", "Binnenhof", [("dijkzigt", "17:03"), ("beurs", "17:06")]),
]


def when(clock: str) -> str:
    hour, minute = map(int, clock.split(":"))
    moment = datetime(2026, 10, 27, hour, minute, tzinfo=AMSTERDAM).astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def place(key: str, clock: str | None = None) -> dict:
    name, (lat, lon), modes = STOPS[key]
    stop = {"stopId": key, "name": name, "lat": lat, "lon": lon}
    if clock:
        stop |= {"departure": when(clock), "scheduledDeparture": when(clock)}
        stop |= {"arrival": when(clock), "scheduledArrival": when(clock)}
    return stop


def hit(key: str) -> dict:
    areas = [{"name": "Rotterdam", "adminLevel": 8, "default": True}]
    return {**place(key), "id": key, "country": "NL", "type": "STOP", "modes": STOPS[key][2], "areas": areas}


def departures(params: dict) -> list[dict]:
    """What leaves from the stop asked, or from every stop within the radius, as Transitous does."""
    here = place(params["stopId"])
    found = []
    for trip, mode, line, headsign, calls in TRIPS:
        for i, (key, clock) in enumerate(calls[:-1]):
            if when(clock) < params.get("time", ""):
                continue
            if key == params["stopId"] or ("radius" in params and ov._distance(here, place(key)) <= params["radius"]):
                st = {"tripId": trip, "mode": mode, "routeShortName": line, "headsign": headsign, "place": place(key, clock)}
                if params.get("fetchStops") == "true":
                    st["nextStops"] = [place(k, c) for k, c in calls[i + 1:]]
                found.append(st)
    return sorted(found, key=lambda st: st["place"]["departure"])


async def get_json(url: str, params: Any = None, **kwargs: Any) -> Any:
    if url == ov.LOCATIESERVER:
        return {"response": {"docs": []}}
    asked.append((url.rsplit("/", 1)[-1], dict(params)))
    if url.endswith("/geocode") and params["type"] == "PLACE":
        return [{"id": "rotterdam", "name": "Rotterdam", "country": "NL", "category": "city", "lat": 51.92, "lon": 4.48}]
    if url.endswith("/geocode"):
        return [hit(key) for key in STOPS]
    return {"stopTimes": departures(params)}


async def uncached(key: tuple, ttl: float, fetch: Any) -> Any:
    return await fetch()


async def no_messages() -> list[str]:
    return []


async def vertrek(halte: str, plaats: str | None = None, **kwargs: Any) -> dict:
    asked.clear()
    return await ov.ov_vertrek(halte, plaats, datum=DAY, **kwargs)


def radii() -> list[int | None]:
    return [params.get("radius") for name, params in asked if name == "stoptimes"]


def rides(result: dict, *keys: str) -> list[tuple]:
    return [tuple(row.get(key) for key in keys) for row in result["vertrektijden"]]


async def main() -> None:
    ov.get_json, ov.cached, ns.berichten = get_json, uncached, no_messages

    # To a place that lends its name to one stop.
    result = await vertrek("Rotterdam Centraal", naar="Erasmus MC", tijd="08:30")
    walks = rides(result, "lijn", "stopt_bij", "aankomst", "lopen_min", "op_bestemming")
    check("naar een plek: ook haltes op loopafstand, met het lopen en hoe laat je er bent, maar geen metro die er niet komt",
          walks == [("44", "Rotterdam, Dijkzigt", "08:41", 3, "08:44"), ("18", "Rotterdam, Kievitslaan", "08:46", 6, "08:52")], walks)
    check("naar een plek: een zin over uitstappen, voor de tijden", list(result)[0] == "uitstappen"
          and "Naar Erasmus MC stap je uit bij de halte in 'stopt_bij'" in result["uitstappen"], result)
    result = await vertrek("Rotterdam Centraal", naar="Dijkzigt", tijd="08:30")
    walks = rides(result, "lijn", "stopt_bij", "lopen_min")
    check("naar een halte met precies die naam: alleen die halte, niet wat er vlakbij stopt, en geen zin over lopen",
          walks == [("44", "Rotterdam, Dijkzigt", None)] and "uitstappen" not in result, walks)

    # From that place.
    result = await vertrek("Erasmus MC", "Rotterdam", naar="Rotterdam Centraal", tijd="17:00")
    check("vanaf een plek: de haltes eromheen in een vraag, met de afstand erbij",
          rides(result, "lijn", "halte", "afstand_m", "stopt_bij")
          == [("44", "Dijkzigt", 130, "Rotterdam Centraal"), ("18", "Kievitslaan", 320, "Rotterdam Centraal")]
          and radii() == [ov.PLACE_RADIUS], (rides(result, "lijn", "halte", "afstand_m", "stopt_bij"), radii()))
    result = await vertrek("Erasmus MC", "Rotterdam", tijd="17:00")
    check("vanaf een plek: een rit langs meer haltes staat er een keer, bij de dichtstbijzijnde",
          rides(result, "lijn", "halte", "afstand_m")
          == [("32", "Erasmus MC Hoofdingang", 0), ("A", "Dijkzigt", 130), ("44", "Dijkzigt", 130), ("18", "Kievitslaan", 320)],
          rides(result, "lijn", "halte", "afstand_m"))
    result = await vertrek("Dijkzigt", "Rotterdam", tijd="17:00")
    check("vanaf een halte met precies die naam: alleen die halte, zonder afstand",
          rides(result, "lijn", "halte", "afstand_m") == [("32", "Dijkzigt", None), ("A", "Dijkzigt", None), ("44", "Dijkzigt", None)]
          and radii() == [None], (rides(result, "lijn", "halte", "afstand_m"), radii()))

    # From a stop asked by its own name, on the way to somewhere.
    result = await vertrek("Erasmus MC Hoofdingang", "Rotterdam", naar="Rotterdam Centraal", tijd="17:00")
    check("vanaf een halte met precies die naam, met naar: ook wat om de hoek erheen gaat, met de afstand",
          rides(result, "lijn", "halte", "afstand_m", "stopt_bij")
          == [("44", "Dijkzigt", 130, "Rotterdam Centraal"), ("18", "Kievitslaan", 320, "Rotterdam Centraal")]
          and radii() == [ov.PLACE_RADIUS], (rides(result, "lijn", "halte", "afstand_m", "stopt_bij"), radii()))
    result = await vertrek("Dijkzigt", "Rotterdam", naar="Rotterdam Centraal", tijd="17:05")
    check("vanaf een halte met precies die naam, met naar: de eigen vertrekken zonder afstand",
          rides(result, "lijn", "halte", "afstand_m") == [("44", "Dijkzigt", None), ("18", "Kievitslaan", 230)],
          rides(result, "lijn", "halte", "afstand_m"))
    result = await vertrek("Dijkzigt", "Rotterdam", naar="Rotterdam Centraal", tijd="17:06")
    check("om de hoek valt weg wat je lopend niet meer haalt: tram 18 gaat om 17:09 en het is 4 minuten lopen",
          rides(result, "lijn", "halte") == [("44", "Dijkzigt")], rides(result, "lijn", "halte"))

    # When 'naar' itself says which stops are meant.
    asked.clear()
    check("naar alleen een station: geen extra zoekvraag", await ov._places(["station"], "station", hit("cs")) == [] and not asked, asked)
    check("naar een plaats: geen plek", await ov._places(["rotterdam"], "Rotterdam", hit("emc")) == [])
    check("naar een station met zijn naam: geen plek", await ov._places(["rotterdam", "centraal"], "Rotterdam Centraal", hit("emc")) == [])
    places = await ov._places(["erasmus", "mc"], "Erasmus MC", hit("cs"))
    check("naar het Erasmus MC: de hoofdingang", [p["name"] for p in places] == ["Rotterdam, Erasmus MC Hoofdingang"], places)

    # A trip at two stops around a place counts once when filling a page.
    evening = {"time": when("17:00")}
    pages = {
        None: (departures({"stopId": "emc", "radius": 400, **evening})[:2], "p2"),
        "p2": (departures({"stopId": "dijkzigt", **evening})[1:2], None),
    }

    async def paged(url: str, params: Any = None, **kwargs: Any) -> Any:
        stop_times, cursor = pages[params.get("pageCursor")]
        return {"stopTimes": stop_times, "nextPageCursor": cursor}

    ov.get_json = paged
    found = await ov._stop_times(hit("emc"), 400, 50, None, lambda st: True, 2, 3)
    check("een rit die bij twee haltes stopt telt een keer: dan volgt de volgende pagina",
          [st["routeShortName"] for st in found] == ["32", "32", "A"], [st["routeShortName"] for st in found])

    print(f"\n{len(failed)} fout" if failed else "\nalles goed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

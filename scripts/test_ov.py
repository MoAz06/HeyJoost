"""Offline checks of how ov_vertrek finds the stops to leave from and to go to, on a made-up piece of Rotterdam
around the Erasmus MC: no network needed.

Run from the project folder: uv run python scripts/test_ov.py
"""

import asyncio
import math
import sys
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

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
    # Only looks like the Museumpark, and lies a kilometre from it.
    "museumpad": ("Rotterdam, Museumpad", spot(1200, 800), ["BUS"]),
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
    ("b40-0833", "BUS", "40", "Kralingen", [("cs", "08:33"), ("museumpad", "08:38")]),
    ("t8-0900", "TRAM", "8", "Spangen", [("euromast", "09:00"), ("kievit", "09:02"), ("eendracht", "09:05")]),
]
# Answers to particular searches, by kind and text: places that no stop is named after, and stops elsewhere.
SEARCHES: dict[tuple[str, str], list[dict]] = {}
# PDOK's streets, by the text asked.
STREETS: dict[str, list[dict]] = {}


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


def found(name: str, town: str, at: tuple[float, float], kind: str = "STOP", category: str = "none", **more: Any) -> dict:
    """A stop or a place anywhere, as the search gives it."""
    areas = [{"name": town, "adminLevel": 8, "default": True}]
    return {"id": f"{name}-{at}", "name": name, "lat": at[0], "lon": at[1], "country": "NL", "type": kind,
            "category": category, "areas": areas, **more}


SEARCHES[("PLACE", "museumpark rotterdam")] = SEARCHES[("PLACE", "museumpark")] = [
    found("Parkeergarage Museumpark", "Rotterdam", spot(260, 160), "PLACE", "parking_16"),
    found("Museumpark", "Rotterdam", spot(250, 150), "PLACE"),
]
SEARCHES[("PLACE", "kunsthal rotterdam")] = [found("Kunsthal", "Rotterdam", spot(-150, 250), "PLACE", "museum_16")]
SEARCHES[("STOP", "hofplein rotterdam")] = [found("Rotterdam, P.C. Hooftplein", "Rotterdam", spot(1000, -2300), modes=["TRAM"])]
STREETS["hofplein rotterdam"] = [{
    "type": "weg", "weergavenaam": "Hofplein, Rotterdam", "straatnaam": "Hofplein", "woonplaatsnaam": "Rotterdam",
    "gemeentenaam": "Rotterdam", "centroide_ll": "POINT(4.4775 51.9240)",
}]
SEARCHES[("STOP", "utrecht science park")] = [
    found("Amsterdam Science Park", "Amsterdam", (52.3523, 4.9505), modes=["REGIONAL_RAIL", "BUS"]),
    found("Utrecht, P+R Science Park", "Utrecht", (52.0915, 5.1778), modes=["TRAM", "BUS"]),
]
# A street with part of that name, in another town.
STREETS["utrecht science park"] = [{
    "type": "weg", "weergavenaam": "Science Park, Amsterdam", "straatnaam": "Science Park", "woonplaatsnaam": "Amsterdam",
    "gemeentenaam": "Amsterdam", "centroide_ll": "POINT(4.9557 52.3560)",
}]
SEARCHES[("STOP", "rotterdam centraal")] = [
    hit("cs"), found("Amsterdam Centraal", "Amsterdam", (52.3792, 4.8994), modes=["REGIONAL_RAIL"]),
]
SEARCHES[("PLACE", "lumc leiden")] = [found("Leidseplein LUMC", "Leiden", (52.1661, 4.4784), "PLACE")]
SEARCHES[("PLACE", "lumc")] = [
    found("LUMC", "Leiden", (52.1669, 4.4653), "PLACE", "office_16"),
    found("LUMC", "Leiden", (52.1671, 4.4745), "PLACE", "hospital_14"),
    found("LUMC Hoofdingang", "Leiden", (52.1663, 4.4790), "PLACE", "entrance_main"),
    found("LUMC Parkeergarage", "Leiden", (52.1679, 4.4765), "PLACE", "parking_16"),
]
SEARCHES[("PLACE", "uithof utrecht")] = [found("De Uithof", "Utrecht", (52.0893, 5.1639), "PLACE")]
SEARCHES[("PLACE", "binnenhof")] = [
    found("Binnenhof", "Leiderdorp", (52.1643, 4.5364), "PLACE", "neighbourhood"),
    found("De Binnenhof", "Leidschendam-Voorburg", (52.0796, 4.3973), "PLACE", "social_facility_14"),
    found("Binnenhof", "Barendrecht", (51.8569, 4.5375), "PLACE", "parking_16"),
]


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
        return {"response": {"docs": STREETS.get(dict(params)["q"].lower(), [])}}
    asked.append((url.rsplit("/", 1)[-1], dict(params)))
    if url.endswith("/geocode") and (params["type"], params["text"].lower()) in SEARCHES:
        return SEARCHES[(params["type"], params["text"].lower())]
    if url.endswith("/geocode") and params["type"] == "PLACE":
        return [{"id": "rotterdam", "name": "Rotterdam", "country": "NL", "category": "city", "lat": 51.92, "lon": 4.48}]
    if url.endswith("/geocode") or url.endswith("/reverse-geocode"):
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

    # A place that no stop is named after.
    result = await vertrek("Museumpark", "Rotterdam", tijd="17:00")
    nearby = [h["halte"] for h in result.get("haltes_in_de_buurt") or []]
    check("vanaf een plek zonder eigen halte: als een adres, met de drie haltes het dichtst bij, niet het Museumpad",
          result.get("adres") == "Museumpark, Rotterdam"
          and nearby == ["Rotterdam, Erasmus MC Hoofdingang", "Rotterdam, Eendrachtsplein", "Rotterdam, Dijkzigt"]
          and rides(result, "lijn", "halte", "afstand_m")
          == [("32", "Erasmus MC Hoofdingang", 290), ("A", "Dijkzigt", 420), ("44", "Dijkzigt", 420), ("18", "Eendrachtsplein", 350)],
          (result.get("adres"), nearby, rides(result, "lijn", "halte", "afstand_m")))
    result = await vertrek("Rotterdam Centraal", naar="Museumpark", tijd="08:30")
    walks = rides(result, "lijn", "stopt_bij", "aankomst", "lopen_min", "op_bestemming")
    check("naar een plek zonder eigen halte: de haltes tot 600 m ervan, met het lopen, en geen bus 40 naar het Museumpad",
          walks == [("44", "Rotterdam, Dijkzigt", "08:41", 7, "08:48"), ("18", "Rotterdam, Eendrachtsplein", "08:42", 6, "08:48")], walks)
    try:
        error = str(await vertrek("Eendrachtsplein", "Rotterdam", naar="Museumpark", tijd="08:40"))
    except ToolError as exc:
        error = str(exc)
    check("naar een plek vlakbij: geen tram 18 die je er verder vandaan brengt, wel hoe lang het lopen is",
          "niks" in error and "Lopen kan wel: dat is ongeveer 6 minuten." in error, error)
    result = await vertrek("Breitnerstraat", "Rotterdam", naar="Erasmus MC", tijd="17:00")
    check("naar een plek op loopafstand: de bus, en dat lopen ook kan",
          rides(result, "lijn", "stopt_bij") == [("32", "Rotterdam, Erasmus MC Hoofdingang")]
          and result.get("lopen") == "Lopen naar Erasmus MC kan ook: dat is ongeveer 5 minuten.",
          (rides(result, "lijn", "stopt_bij"), result.get("lopen")))
    result = await vertrek("Dijkzigt", "Rotterdam", naar="Erasmus MC", tijd="08:55")
    check("naar een plek vlakbij: geen tram 8 om de hoek naar een halte verder van de plek dan waar je bent",
          rides(result, "lijn", "halte", "stopt_bij") == [("32", "Breitnerstraat", "Rotterdam, Erasmus MC Hoofdingang")]
          and result.get("lopen") == "Lopen naar Erasmus MC kan ook: dat is ongeveer 3 minuten.",
          (rides(result, "lijn", "halte", "stopt_bij"), result.get("lopen")))
    point, best, _, _ = await ov._origin("Kunsthal", "Rotterdam")
    check("een plek zonder halte en zonder straat: de plek zelf", bool(point) and point["naam"] == "Kunsthal, Rotterdam", point)
    point, best, _, _ = await ov._origin("Hofplein", "Rotterdam")
    check("een halte die er alleen op lijkt (P.C. Hooftplein) wijkt voor de straat met precies die naam",
          bool(point) and point["naam"] == "Hofplein, Rotterdam" and best is None, (point, best and best["name"]))
    point = await ov._spot("LUMC", "Leiden")
    check("een plek met een hoofdingang: de hoofdingang, niet het kantoor of de parkeergarage",
          bool(point) and point["naam"] == "LUMC Hoofdingang, Leiden", point)
    point = await ov._spot("Uithof", "Utrecht")
    check("De Uithof is de Uithof", bool(point) and point["naam"] == "De Uithof, Utrecht", point)
    try:
        error = str(await ov._spot("Binnenhof", None))
    except ToolError as exc:
        error = str(exc)
    check("een plek in meer plaatsen zonder plaats erbij: vraag de plaats, parkeerplaatsen tellen niet",
          "meer dan één plaats" in error and "Leiderdorp" in error and "Barendrecht" not in error, error)

    # Which town and which station is meant.
    best, others, _ = await ov._find("Utrecht Science Park", None)
    check("een plaats in de naam telt: Utrecht Science Park is niet Amsterdam Science Park",
          best["name"] == "Utrecht, P+R Science Park" and [h["name"] for h in others] == ["Utrecht, P+R Science Park"],
          (best["name"], [h["name"] for h in others]))
    point, best, _, _ = await ov._origin("Utrecht Science Park", None)
    check("een straat met een deel van de naam telt alleen in de plaats uit de naam: niet Science Park in Amsterdam",
          point is None and best["name"] == "Utrecht, P+R Science Park", (point, best and best["name"]))
    best, others, _ = await ov._find("Rotterdam Centraal", None)
    check("andere haltes alleen uit de plaats die gevraagd is: geen Amsterdam Centraal",
          best["name"] == "Rotterdam Centraal" and [h["name"] for h in others] == ["Rotterdam Centraal"], [h["name"] for h in others])
    stations = [
        ("Utrecht Centraal", "Utrecht, Centraal Museum", False),
        ("station Utrecht", "Utrecht, Centraal Museum", False),
        ("Utrecht Centraal", "Utrecht, CS Centrumzijde", True),
        ("Leiden Centraal", "Leiden, Leiden Centraal Westzijde", True),
        ("Rotterdam Centraal", "Rotterdam, Rotterdam Centraal (uitstaphalte)", True),
        ("Den Haag Centraal", "'s-Gravenhage, Den Haag Centraal", True),
        ("Arnhem Centraal", "Arnhem, Centraal Stat.(Sonsbeek)", True),
        ("station Hilversum", "Hilversum, Station Hilversum", True),
        ("station Maastricht", "Maastricht, Station (Bus)", True),
        ("station Haarlem", "Haarlem, Station Spaarnwoude", False),
    ]
    wrong = [(naar, name) for naar, name, meant in stations if ov._is_target(ov._words(naar), name) != meant]
    check("naar een station: zijden, haakjes en de plaatsnaam mogen erbij, maar Centraal Museum is geen station", not wrong, wrong)
    looks = [
        (["hofplein"], "Rotterdam, P.C. Hooftplein", True),
        (["kurhaus"], "Den Haag, Kurhausweg", True),
        (["kurhaus", "scheveningen"], "Den Haag, Kurhaus", False),
        (["erasmus", "mc"], "Rotterdam, Erasmus MC Hoofdingang", False),
    ]
    wrong = [(words, name) for words, name, meant in looks if ov._lookalike(words, name) != meant]
    check("lijkt er alleen op: Hooftplein voor Hofplein en Kurhausweg voor Kurhaus, maar Kurhaus is Kurhaus Scheveningen", not wrong, wrong)

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

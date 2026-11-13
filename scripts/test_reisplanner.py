"""Offline checks of where the door-to-door planner starts and ends, and how many plans it asks: made-up NS
stations, a made-up PDOK and a made-up Transitous, no key and no network needed.

Run from the project folder: uv run python scripts/test_reisplanner.py
"""

import asyncio
import re
import sys
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from nederland_mcp.bronnen import ns, ov, plaats, reisplanner

failed: list[str] = []
# The calls to Transitous of the last check, and of all checks, as (endpoint, params).
asked: list[tuple[str, dict]] = []
everything: list[tuple[str, dict]] = []


def check(name: str, ok: bool, got: Any = None) -> None:
    print(f"{'ok  ' if ok else 'FOUT'} {name}" + ("" if ok else f": {got}"))
    if not ok:
        failed.append(name)


def station(name: str, kind: str, lat: float, lng: float, *synonyms: str) -> dict:
    """A station as NS lists it."""
    return {
        "code": name[:3].upper(), "namen": {"lang": name}, "synoniemen": list(synonyms), "land": "NL",
        "stationType": kind, "heeftVertrektijden": True, "lat": lat, "lng": lng,
    }


STATIONS = [
    station("Utrecht Centraal", "MEGA_STATION", 52.0894, 5.1101),
    station("Utrecht Lunetten", "STOPTREIN_STATION", 52.0614, 5.1394),
    station("Bunnik", "STOPTREIN_STATION", 52.0624, 5.1972),
    station("Den Haag Centraal", "MEGA_STATION", 52.0802, 4.3249, "Den Haag CS"),
    station("Den Haag HS", "KNOOPPUNT_INTERCITY_STATION", 52.0696, 4.3226, "Den Haag Hollands Spoor"),
    station("Nijmegen", "KNOOPPUNT_INTERCITY_STATION", 51.8434, 5.8536),
    station("Nijmegen Heyendaal", "STOPTREIN_STATION", 51.8265, 5.8691),
    station("Leiden Centraal", "KNOOPPUNT_INTERCITY_STATION", 52.1664, 4.4817),
    station("Amsterdam Science Park", "STOPTREIN_STATION", 52.3536, 4.9488),
    station("Rheden", "STOPTREIN_STATION", 52.0089, 6.0300),
    station("Ede-Wageningen", "INTERCITY_STATION", 52.0275, 5.6741),
]


def found(name: str, town: str, lat: float, lon: float, kind: str = "STOP", category: str = "none") -> dict:
    """A stop or a place as Transitous' search gives it."""
    areas = [{"name": town, "adminLevel": 8, "default": True}]
    return {"id": f"{name}-{lat}", "name": name, "lat": lat, "lon": lon, "country": "NL", "type": kind,
            "category": category, "areas": areas, "modes": ["TRAM", "BUS"]}


# Answers to particular searches at Transitous, by kind and text.
SEARCHES: dict[tuple[str, str], list[dict]] = {
    ("STOP", "kurhaus den haag"): [found("Den Haag, Kurhaus", "'s-Gravenhage", 52.1116, 4.2847)],
    ("PLACE", "den haag"): [found("Den Haag", "'s-Gravenhage", 52.0799, 4.3113, "PLACE", "city")],
    ("PLACE", "leiden"): [found("Leiden", "Leiden", 52.1583, 4.4931, "PLACE", "city")],
    ("PLACE", "utrecht"): [found("Utrecht", "Utrecht", 52.0907, 5.1214, "PLACE", "city")],
    ("PLACE", "lumc leiden"): [
        found("LUMC", "Leiden", 52.1671, 4.4745, "PLACE", "hospital_14"),
        found("LUMC Hoofdingang", "Leiden", 52.1663, 4.4790, "PLACE", "entrance_main"),
    ],
}
# PDOK's addresses and streets, by the text asked. The address of a public building: the Erasmus MC.
STREETS: dict[str, list[dict]] = {
    "dr. molewaterplein 40 rotterdam": [{
        "type": "adres", "weergavenaam": "Dr. Molewaterplein 40, 3015GD Rotterdam", "straatnaam": "Dr. Molewaterplein",
        "postcode": "3015GD", "woonplaatsnaam": "Rotterdam", "gemeentenaam": "Rotterdam",
        "centroide_ll": "POINT(4.468644 51.911926)",
    }],
}
# The plans Transitous gives, in order; an empty list is a plan without trips.
PLANS: list[list[dict]] = []


async def get_json(url: str, params: Any = None, **kwargs: Any) -> Any:
    if url == ov.LOCATIESERVER:
        return {"response": {"docs": STREETS.get(dict(params)["q"].lower(), [])}}
    params = dict(params or {})
    asked.append((url.rsplit("/", 1)[-1], params))
    everything.append(asked[-1])
    if url.endswith("/plan"):
        return {"itineraries": PLANS.pop(0) if PLANS else []}
    if url.endswith("/reverse-geocode"):
        lat, lon = map(float, params["place"].split(","))
        return [found("Rotterdam, Erasmus MC Hoofdingang", "Rotterdam", lat + 0.0003, lon)]
    return SEARCHES.get((params["type"], params["text"].lower()), [])


async def uncached(key: tuple, ttl: float, fetch: Any) -> Any:
    return await fetch()


async def stations() -> list[dict]:
    return STATIONS


async def zoek(text: str) -> plaats.Plaats:
    if text.lower() == "wassenaar":
        return plaats.Plaats("Wassenaar", 52.14321, 4.40112, "Wassenaar", "Zuid-Holland", "woonplaats")
    raise ToolError("niet gevonden")


def leaks() -> list[str]:
    """What Transitous got that is or holds an address: a text with a house number or postcode, or a point more
    precise than about a hundred metres."""
    wrong = []
    for name, params in everything:
        if name == "geocode" and ov._is_address(params.get("text", "")):
            wrong.append(params["text"])
        for key in ("place", "fromPlace", "toPlace"):
            # A point is two numbers; a stop goes by its id.
            point = re.fullmatch(r"-?\d+\.(\d+),-?\d+\.(\d+)", params.get(key, ""))
            if point and max(len(point[1]), len(point[2])) > 3 and point[0] not in PUBLIC:
                wrong.append(point[0])
    return wrong


# Points that are public places, so they may be precise: the stations and spots in this test.
PUBLIC = {f"{s['lat']:.5f},{s['lng']:.5f}" for s in STATIONS} | {"52.16630,4.47900"}


async def point(text: str, town: str | None = None) -> reisplanner._Place | str:
    asked.clear()
    try:
        return await reisplanner._point(text, town)
    except ToolError as exc:
        return str(exc)


def trip(start: str, end: str) -> dict:
    """One trip of a plan, walking only, enough for the answer."""
    return {"startTime": start, "endTime": end, "duration": 1800, "transfers": 0,
            "legs": [{"mode": "WALK", "duration": 1800, "distance": 2000, "from": {"name": "START"}, "to": {"name": "END"}}]}


async def budget() -> None:
    """In a task of its own, as a chat question is, so its count stays out of the other checks."""
    reisplanner._today.update(dag=None, aantal=0)
    reisplanner.new_question()
    asked.clear()
    # The first plan from the address has no trips, so reis_plannen asks once more from the stop nearest to it.
    PLANS[:] = [[], [trip("2026-11-14T08:00:00Z", "2026-11-14T08:30:00Z")]]
    result = await reisplanner.reis_plannen("Dr. Molewaterplein 40 Rotterdam", "Utrecht", tijd="09:00")
    plans = [params for name, params in asked if name == "plan"]
    check("de herkansing is een tweede plan, van de halte bij het adres", len(plans) == 2 and "let_op" in result,
          (len(plans), result.get("let_op")))
    try:
        error = str(await reisplanner.reis_plannen("Utrecht", "Nijmegen", tijd="10:00"))
    except ToolError as exc:
        error = str(exc)
    plans = [params for name, params in asked if name == "plan"]
    check("een derde plan in dezelfde vraag wordt niet gevraagd, de herkansing telt mee",
          len(plans) == 2 and "genoeg reizen gepland" in error, (len(plans), error))
    check("de dag telt alleen de plannen die echt gevraagd zijn", reisplanner._today["aantal"] == 2, reisplanner._today)

    # Lookups side by side share the count of their question.
    reisplanner.new_question()
    asked.clear()
    PLANS[:] = [[trip("2026-11-14T08:00:00Z", "2026-11-14T08:30:00Z")]] * 3

    async def plan(van: str, naar: str) -> str:
        try:
            await reisplanner.reis_plannen(van, naar, tijd="10:00")
            return "plan"
        except ToolError as exc:
            return str(exc)

    results = await asyncio.gather(plan("Utrecht", "Nijmegen"), plan("Nijmegen", "Leiden Centraal"), plan("Den Haag", "Utrecht"))
    check("drie plannen tegelijk in een vraag: twee gaan door", results.count("plan") == 2, results)


async def main() -> None:
    ov.get_json, ov.cached = get_json, uncached
    reisplanner.get_json, reisplanner.cached = get_json, uncached
    ns._stations = stations
    plaats.zoek = zoek

    # A town or a station is the NS station, the same one trein_reis takes, without asking Transitous.
    for text, town, meant in [
        ("Utrecht", None, "Utrecht Centraal"),
        ("Den Haag", None, "Den Haag Centraal"),
        ("Nijmegen", None, "Nijmegen"),
        ("Leiden Centraal", "Leiden", "Leiden Centraal"),
        ("Science Park", "Amsterdam", "Amsterdam Science Park"),
    ]:
        where = await point(text, town)
        ns_station = next(s for s in STATIONS if s["namen"]["lang"] == meant)
        check(f"{text}{' in ' + town if town else ''} is station {meant}, op het punt van NS",
              not isinstance(where, str) and where.name == meant
              and where.place == f"{ns_station['lat']:.5f},{ns_station['lng']:.5f}"
              and not any(name == "geocode" for name, _ in asked),
              where if isinstance(where, str) else (where.name, where.place, asked))
    where = await point("Science Park", "Utrecht")
    check("Science Park in Utrecht is niet het station in Amsterdam",
          isinstance(where, str) or where.name != "Amsterdam Science Park", where if isinstance(where, str) else where.name)
    where = await point("Rheden", "Ede")
    check("hele woorden: station Rheden ligt niet in Ede", isinstance(where, str) or where.name != "Rheden",
          where if isinstance(where, str) else where.name)

    # A stop with its own name, and a place that no stop is named after.
    where = await point("Kurhaus", "Den Haag")
    check("Kurhaus is de halte zelf", not isinstance(where, str) and where.place == "Den Haag, Kurhaus-52.1116"
          and where.name == "Den Haag, Kurhaus", where if isinstance(where, str) else (where.place, where.name))
    where = await point("LUMC", "Leiden")
    check("het LUMC is de hoofdingang", not isinstance(where, str) and where.place == "52.16630,4.47900",
          where if isinstance(where, str) else (where.place, where.name))
    where = await point("Wassenaar")
    check("een plaats zonder station: het midden, op 100 meter", not isinstance(where, str) and where.place == "52.143,4.401",
          where if isinstance(where, str) else (where.place, where.name))

    # An address goes to PDOK only, and to Transitous as a point of about a hundred metres.
    where = await point("Dr. Molewaterplein 40 Rotterdam")
    check("een adres: een punt op 100 meter, en de naam zonder huisnummer of postcode",
          not isinstance(where, str) and where.place == "51.912,4.469" and where.name == "Dr. Molewaterplein, Rotterdam",
          where if isinstance(where, str) else (where.place, where.name))
    where = await point("Nergensstraat 12", "Rotterdam")
    check("een adres dat PDOK niet kent: een fout, en niets naar Transitous",
          isinstance(where, str) and "Geen adres gevonden" in where and not asked, (where, asked))

    await asyncio.create_task(budget())
    # Outside a chat question, as for an MCP client, only the cap of the day counts.
    reisplanner._today.update(dag=None, aantal=0)
    PLANS[:] = [[trip("2026-11-14T08:00:00Z", "2026-11-14T08:30:00Z")]] * 3
    done = 0
    for _ in range(3):
        await reisplanner.reis_plannen("Utrecht", "Nijmegen", tijd="10:00")
        done += 1
    check("buiten de chat geen grens per vraag", done == 3)

    check("niets naar Transitous dat een adres is of bevat", not leaks(), leaks())
    print(f"\n{len(failed)} fout" if failed else "\nalles goed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

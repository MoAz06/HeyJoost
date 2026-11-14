"""Offline checks of where the door-to-door planner starts and ends, the trips it gives, and how many plans it asks:
made-up NS stations, a made-up PDOK and a made-up Transitous, no key and no network needed.

Run from the project folder: uv run python scripts/test_reisplanner.py
"""

import asyncio
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from nederland_mcp.bronnen import ns, ov, plaats, reisplanner
from nederland_mcp.common import AMSTERDAM

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
    # Scheveningen is a district, not a woonplaats: the Kurhaus is found around it, as ov_vertrek does.
    ("STOP", "kurhaus scheveningen"): [found("'s-Gravenhage, Kurhausweg", "'s-Gravenhage", 52.1101, 4.2919)],
    ("STOP", "kurhaus"): [found("Den Haag, Kurhaus", "'s-Gravenhage", 52.1116, 4.2847)],
    ("PLACE", "scheveningen"): [found("Scheveningen", "'s-Gravenhage", 52.1067, 4.2737, "PLACE", "suburb")],
    # The village in OpenStreetMap, where PDOK has the middle of its area in the dunes.
    ("PLACE", "wassenaar"): [found("Wassenaar", "Wassenaar", 52.1428, 4.4011, "PLACE", "town")],
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
    """PDOK: the middle of an area, for Wassenaar in the dunes, and Scheveningen as a wijk of Den Haag."""
    if text.lower() == "wassenaar":
        return plaats.Plaats("Wassenaar", 52.14106, 4.36705, "Wassenaar", "Zuid-Holland", "woonplaats")
    if text.lower() == "noordeinde":
        return plaats.Plaats("Noordeinde", 52.30001, 4.70001, "Ergens", "Noord-Holland", "woonplaats")
    if "scheveningen" in text.lower():
        return plaats.Plaats("Scheveningen, Den Haag", 52.1042, 4.2734, "'s-Gravenhage", "Zuid-Holland", "wijk")
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


def minutes(clock: str) -> int:
    hour, minute = map(int, clock.split(":"))
    return hour * 60 + minute


def leg(mode: str, start: str, end: str, begins: str, ends: str, **extra: Any) -> dict:
    """A leg of a plan as Transitous gives it, on 17 November, with times in UTC: an hour before Amsterdam."""
    track = {"track": extra.pop("track")} if "track" in extra else {}
    begins, ends = f"2026-11-17T{begins}:00Z", f"2026-11-17T{ends}:00Z"
    return {"mode": mode, "from": {"name": start, **track}, "to": {"name": end},
            "duration": (minutes(ends[11:16]) - minutes(begins[11:16])) * 60, "startTime": begins, "endTime": ends,
            "scheduledStartTime": begins, "scheduledEndTime": ends, "realTime": False, **extra}


def to_kurhaus(shift: int = 0) -> dict:
    """From Utrecht Centraal by train and tram 9 to the Kurhaus, with the walk from the station point to its platform
    first; `shift` minutes earlier."""
    def at(clock: str) -> str:
        total = minutes(clock) - shift
        return f"{total // 60:02d}:{total % 60:02d}"

    legs = [
        leg("WALK", "START", "Utrecht Centraal", at("11:53"), at("11:55")),
        leg("REGIONAL_RAIL", "Utrecht Centraal", "Den Haag Centraal", at("11:55"), at("12:33"), track="5",
            displayName="Intercity", routeShortName="Intercity", headsign="Den Haag Centraal", agencyName="NS"),
        leg("WALK", "Den Haag Centraal", "Den Haag, Centraal Station", at("12:33"), at("12:37")),
        leg("TRAM", "Den Haag, Centraal Station", "END", at("12:39"), at("12:50"), track="E", routeShortName="9",
            headsign="Scheveningen Noorderstrand", agencyName="HTM"),
    ]
    return {"startTime": legs[0]["startTime"], "endTime": legs[-1]["endTime"], "duration": 3420, "transfers": 1,
            "legs": legs}


def tight_trip() -> dict:
    """Three minutes sooner at the Kurhaus than to_kurhaus, with two trams and both transfers tight."""
    legs = [
        leg("WALK", "START", "Utrecht Centraal", "11:53", "11:55"),
        leg("REGIONAL_RAIL", "Utrecht Centraal", "Den Haag Centraal", "11:55", "12:31", track="5",
            displayName="Intercity", routeShortName="Intercity", headsign="Den Haag Centraal", agencyName="NS"),
        leg("WALK", "Den Haag Centraal", "Den Haag, Centraal Station", "12:31", "12:33"),
        leg("TRAM", "Den Haag, Centraal Station", "Den Haag, Javabrug", "12:34", "12:38", routeShortName="1"),
        leg("WALK", "Den Haag, Javabrug", "Den Haag, Javabrug", "12:38", "12:39"),
        leg("TRAM", "Den Haag, Javabrug", "END", "12:40", "12:47", routeShortName="9"),
    ]
    return {"startTime": legs[0]["startTime"], "endTime": legs[-1]["endTime"], "duration": 3240, "transfers": 2,
            "legs": legs}


def by_icd(trip: dict, number: str = "1820") -> dict:
    """The same trip with an Intercity direct for its train."""
    legs = [dict(part) for part in trip["legs"]]
    legs[1] |= {"displayName": f"ICD {number}", "routeShortName": "ICD", "tripShortName": number}
    return {**trip, "legs": legs}


def ns_trip(clock: str, number: str, cents: int | None) -> dict:
    """A direct train from Utrecht Centraal to Den Haag Centraal on 17 November as NS gives it."""
    fare = {"priceInCents": 1500, **({"supplementInCents": cents} if cents else {})}
    return {"legs": [{"travelType": "PUBLIC_TRANSIT", "product": {"number": number},
                      "origin": {"name": "Utrecht Centraal", "plannedDateTime": f"2026-11-17T{clock}:00+0100"},
                      "destination": {"name": "Den Haag Centraal", "plannedDateTime": "2026-11-17T13:33:00+0100"}}],
            "transfers": 0, "productFare": fare}


async def supplements() -> None:
    """The supplement of a train in a plan comes from NS, asked once per train, and is not there when NS cannot say."""
    os.environ.setdefault("NS_API_KEY", "offline")
    seen: list[dict] = []

    async def ns_json(url: str, params: Any = None, **kwargs: Any) -> dict:
        seen.append(dict(params or {}))
        return {"trips": [ns_trip("12:48", "1818", 270), ns_trip("12:55", "3100", None), ns_trip("12:55", "1820", 270)]}

    real = ns.get_json, ns.cached
    ns.get_json, ns.cached = ns_json, uncached
    try:
        leaves = datetime(2026, 11, 17, 12, 55, tzinfo=AMSTERDAM)
        amounts = [await ns._supplement("Utrecht Centraal", "Den Haag Centraal", leaves, number)
                   for number in ("1820", "3100")]
        amounts.append(await ns._supplement("Utrecht Centraal", "Den Haag Centraal", leaves + timedelta(minutes=30)))
    finally:
        ns.get_json, ns.cached = real
    check("NS geeft de toeslag van precies die trein: zelfde tijd en nummer, niet de trein ervoor of een andere",
          amounts == [2.7, None, None] and seen[0] == {"fromStation": "UTR", "toStation": "DEN",
                                                       "dateTime": "2026-11-17T12:55:00+01:00", "searchForArrival": "false"},
          (amounts, seen[:1]))

    calls: list[tuple] = []
    answers: list[Any] = []

    async def supplement(*train: Any) -> float | None:
        calls.append(train)
        answer = answers.pop(0) if answers else 2.7
        if isinstance(answer, Exception):
            raise answer
        return answer

    real_supplement = ns._supplement
    ns._supplement = supplement
    try:
        bus = by_icd(to_kurhaus())
        bus["legs"] = [*bus["legs"][:3], {**bus["legs"][3], "mode": "BUS", "routeShortName": "22"}]
        PLANS[:] = [[by_icd(to_kurhaus()), bus, to_kurhaus(20)]]
        result = await reisplanner.deur_tot_deur("Utrecht", "Kurhaus Den Haag", "2026-11-17", "14:00", True)
        toeslag = [trip.get("toeslag_euro") for trip in result["reisadviezen"]]
        check("een ICD in de reis: de toeslag van NS staat erbij, één keer gevraagd voor twee reizen met die trein",
              len(calls) == 1 and calls[0][3] == "1820" and calls[0][:2] == ("Utrecht Centraal", "Den Haag Centraal")
              and sorted(toeslag, key=str) == [2.7, 2.7, None], (calls, toeslag))
        calls.clear()
        answers.append(RuntimeError("NS ligt plat"))
        PLANS[:] = [[by_icd(to_kurhaus())]]
        result = await reisplanner.deur_tot_deur("Utrecht", "Kurhaus Den Haag", "2026-11-17", "14:00", True)
        check("weet NS het niet, dan de reis zonder toeslag, en geen fout",
              len(calls) == 1 and "toeslag_euro" not in result["reisadviezen"][0], result["reisadviezen"][0])
        calls.clear()
        PLANS[:] = [[to_kurhaus()]]
        await reisplanner.deur_tot_deur("Utrecht", "Kurhaus Den Haag", "2026-11-17", "14:00", True)
        check("een gewone Intercity: NS wordt niets gevraagd", not calls, calls)
    finally:
        ns._supplement = real_supplement


async def budget() -> None:
    """In a task of its own, as a chat question is, so its count stays out of the other checks."""
    reisplanner._today.update(dag=None, aantal=0)
    reisplanner.new_question()
    asked.clear()
    PLANS[:] = [[to_kurhaus()]] * 3

    async def plan() -> str:
        try:
            await reisplanner.deur_tot_deur("Utrecht", "Kurhaus Den Haag", "2026-11-17", "14:00", True)
            return "plan"
        except ToolError as exc:
            return str(exc)

    results = [await plan(), await plan(), await plan()]
    plans = [params for name, params in asked if name == "plan"]
    check("een derde plan in dezelfde vraag wordt niet gevraagd", results[:2] == ["plan", "plan"]
          and "genoeg reizen gepland" in results[2] and len(plans) == 2, (results, len(plans)))
    check("de dag telt alleen de plannen die echt gevraagd zijn", reisplanner._today["aantal"] == 2, reisplanner._today)

    # Lookups side by side share the count of their question.
    reisplanner.new_question()
    PLANS[:] = [[to_kurhaus()]] * 3
    results = list(await asyncio.gather(plan(), plan(), plan()))
    check("drie plannen tegelijk in een vraag: twee gaan door", results.count("plan") == 2, results)


TOWNS = {"utrecht", "den haag", "leiden", "rotterdam", "nijmegen"}


async def is_town(name: str) -> bool:
    return name.lower() in TOWNS


async def main() -> None:
    ov.get_json, ov.cached = get_json, uncached
    reisplanner.get_json, reisplanner.cached = get_json, uncached
    ns._stations = stations
    plaats.zoek, plaats.is_town = zoek, is_town

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
    check("een plaats zonder station: het dorp in OpenStreetMap en niet het midden van PDOK in de duinen, op 100 meter",
          not isinstance(where, str) and where.place == "52.143,4.401",
          where if isinstance(where, str) else (where.place, where.name))
    where = await point("Noordeinde")
    check("een plaats die OpenStreetMap niet kent: het midden van PDOK", not isinstance(where, str)
          and where.place == "52.300,4.700", where if isinstance(where, str) else (where.place, where.name))
    where = await point("Kurhaus Scheveningen")
    check("met een wijk die geen woonplaats is: de halte in die wijk, niet het midden van Scheveningen",
          not isinstance(where, str) and where.place == "Den Haag, Kurhaus-52.1116" and where.name == "Den Haag, Kurhaus",
          where if isinstance(where, str) else (where.place, where.name))

    # An address goes to PDOK only, and to Transitous as a point of about a hundred metres.
    where = await point("Dr. Molewaterplein 40 Rotterdam")
    check("een adres: een punt op 100 meter, en de naam zonder huisnummer of postcode",
          not isinstance(where, str) and where.place == "51.912,4.469" and where.name == "Dr. Molewaterplein, Rotterdam",
          where if isinstance(where, str) else (where.place, where.name))
    where = await point("Nergensstraat 12", "Rotterdam")
    check("een adres dat PDOK niet kent: een fout, en niets naar Transitous",
          isinstance(where, str) and "Geen adres gevonden" in where and not asked, (where, asked))

    async def kurhaus(tijd: str, aankomst: bool) -> dict | str:
        try:
            return await reisplanner.deur_tot_deur("Utrecht", "Kurhaus Den Haag", "2026-11-17", tijd, aankomst)
        except ToolError as exc:
            return str(exc)

    # To be there at a time: one plan for 5 minutes before it, in the shape of trein_reis.
    reisplanner._today.update(dag=None, aantal=0)
    asked.clear()
    PLANS[:] = [[to_kurhaus(), to_kurhaus(20), to_kurhaus(-10)]]
    result = await kurhaus("14:00", True)
    plans = [params for name, params in asked if name == "plan"]
    params = plans[0] if plans else {}
    check("een plan, aankomst 5 minuten voor de tijd, met 2 minuten extra per overstap",
          len(plans) == 1 and params.get("arriveBy") == "true" and params.get("time") == "2026-11-17T12:55:00Z"
          and params.get("additionalTransferTime") == 2 and params.get("fromPlace") == "52.08940,5.11010"
          and params.get("toPlace") == "Den Haag, Kurhaus-52.1116", plans)
    result = result if isinstance(result, dict) else {"fout": result}
    check("het resultaat komt van de reisplanner, met de gevraagde tijd en de marge",
          result.get("reisplanner") is True and result.get("gevraagd") == "aankomst 2026-11-17 14:00"
          and result.get("gezocht_op") == "aankomst 2026-11-17 13:55"
          and result.get("op_tijd_voor") == "14:00, met 5 minuten marge"
          and (result.get("van"), result.get("naar")) == ("Utrecht Centraal", "Den Haag, Kurhaus"), result)
    trips = result.get("reisadviezen") or []
    check("een reis die na 13:55 aankomt valt af, en de laatste vertrekker is de beste",
          [trip.get("vertrek") for trip in trips] == ["12:55", "12:35"] and trips[0].get("beste") is True
          and "beste" not in trips[1], [(trip.get("vertrek"), trip.get("aankomst"), trip.get("beste")) for trip in trips])
    first = trips[0] if trips else {}
    parts = first.get("delen") or []
    shape = [part.get("soort") or ("trein" if "trein" in part else "lopen" if "lopen_min" in part else "?") for part in parts]
    check("trein, overstap lopen, tram 9, zonder het loopje van het stationspunt naar het perron",
          shape == ["trein", "lopen", "tram"] and parts[0].get("spoor") == "5" and parts[1].get("overstap") is True
          and parts[1].get("lopen_min") == 4 and (parts[2].get("lijn"), parts[2].get("perron")) == ("9", "E")
          and parts[2].get("naar") == "Den Haag, Kurhaus" and parts[2].get("vervoerder") == "HTM", parts)
    check("de tijden van de reis zijn Nederlandse tijd, van het eerste tot het laatste stuk",
          (first.get("vertrek"), first.get("aankomst"), first.get("reistijd_min")) == ("12:55", "13:50", 55),
          (first.get("vertrek"), first.get("aankomst"), first.get("reistijd_min")))
    check("de overstap telt van trein tot tram, met het lopen erin",
          first.get("overstaptijd") == [ns._change_line("Den Haag Centraal", 6)], first.get("overstaptijd"))

    # Leaving at a time: no trip that has already left, the first one there first, and no margin.
    asked.clear()
    PLANS[:] = [[to_kurhaus(-30), to_kurhaus(10), to_kurhaus()]]
    result = await kurhaus("12:53", False)
    result = result if isinstance(result, dict) else {"fout": result}
    trips = result.get("reisadviezen") or []
    params = next((params for name, params in asked if name == "plan"), {})
    check("vertrekken: een reis die al weg is valt af, de eerste die aankomt is de beste, en geen marge",
          [trip.get("vertrek") for trip in trips] == ["12:55", "13:25"] and trips[0].get("beste") is True
          and "op_tijd_voor" not in result and result.get("gezocht_op") == "vertrek 2026-11-17 12:53"
          and params.get("arriveBy") == "false" and params.get("time") == "2026-11-17T11:53:00Z",
          ([trip.get("vertrek") for trip in trips], result.get("gezocht_op"), params))

    PLANS[:] = [[tight_trip(), to_kurhaus()]]
    result = await kurhaus("14:00", True)
    trips = result.get("reisadviezen") or [] if isinstance(result, dict) else []
    check("zoals bij NS telt een overstap als 5 minuten en een krappe dubbel: 3 minuten eerder met twee krappe "
          "overstappen is niet de beste", [trip.get("aankomst") for trip in trips] == ["13:50", "13:47"],
          [(trip.get("aankomst"), trip.get("overstaptijd")) for trip in trips])

    # Someone who will not pay a supplement: the plan leaves the fast trains out, and only then.
    asked.clear()
    PLANS[:] = [[to_kurhaus()], [to_kurhaus()]]
    await kurhaus("14:00", True)
    await reisplanner.deur_tot_deur("Utrecht", "Kurhaus Den Haag", "2026-11-17", "14:00", True, True)
    plans = [params for name, params in asked if name == "plan"]
    modes = set((plans[-1].get("transitModes") or "").split(",")) if len(plans) == 2 else set()
    check("zonder toeslag laat de planner de Intercity direct (LONG_DISTANCE) en de Eurostar (HIGHSPEED_RAIL) weg, "
          "met wel Intercity, Sprinter, bus, tram en metro; anders mag alles",
          len(plans) == 2 and "transitModes" not in plans[0] and {"REGIONAL_RAIL", "BUS", "TRAM", "SUBWAY"} <= modes
          and not modes & {"LONG_DISTANCE", "HIGHSPEED_RAIL", "RAIL", "TRANSIT"}, plans)
    await supplements()

    PLANS[:] = [[to_kurhaus(-10)]]
    error = await kurhaus("14:00", True)
    check("past geen reis, dan een fout, zodat trein_reis de trein neemt",
          isinstance(error, str) and "vond geen reis" in error, error)
    asked.clear()
    try:
        error = str(await reisplanner.deur_tot_deur("Utrecht", "Utrecht Centraal", None, "14:00", True))
    except ToolError as exc:
        error = str(exc)
    check("begin en eind op dezelfde plek: een fout, en geen plan",
          "dezelfde plek" in error and not any(name == "plan" for name, _ in asked), (error, asked))

    await asyncio.create_task(budget())
    # Outside a chat question, as for an MCP client, only the cap of the day counts.
    reisplanner._today.update(dag=None, aantal=0)
    PLANS[:] = [[to_kurhaus()]] * 3
    results = [await kurhaus("14:00", True) for _ in range(3)]
    check("buiten de chat geen grens per vraag", all(isinstance(result, dict) for result in results), results)
    reisplanner._today["aantal"] = reisplanner._per_day()
    error = await kurhaus("14:00", True)
    check("aan het maximum van de dag: een fout", isinstance(error, str) and "maximum" in error, error)
    reisplanner._today.update(dag=None, aantal=0)

    # reis_plannen is trein_reis, with the town after the name.
    calls: list[tuple] = []
    real = ns.trein_reis

    async def fake(*args: Any, **kwargs: Any) -> dict:
        calls.append((*args, kwargs))
        return {"via": "trein_reis"}

    ns.trein_reis = fake
    try:
        result = await reisplanner.reis_plannen("Kurhaus", "Science Park", "Den Haag", "Amsterdam", tijd="10:00")
        await reisplanner.reis_plannen("Utrecht Centraal", "Leiden", van_plaats="Utrecht", tijd="09:00", aankomst=True,
                                       zonder_toeslag=True)
    finally:
        ns.trein_reis = real
    check("reis_plannen is trein_reis, met de plaats achter de naam als die er nog niet staat, en zonder toeslag",
          result == {"via": "trein_reis"} and calls == [
              ("Kurhaus Den Haag", "Science Park Amsterdam", None, "10:00", False, {"zonder_toeslag": False}),
              ("Utrecht Centraal", "Leiden", None, "09:00", True, {"zonder_toeslag": True}),
          ], calls)

    check("niets naar Transitous dat een adres is of bevat", not leaks(), leaks())
    print(f"\n{len(failed)} fout" if failed else "\nalles goed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

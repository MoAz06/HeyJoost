"""Offline checks of how the train tools find a station by name, and what they say for a place without one, on
made-up NS stations and a made-up Transitous: no key and no network needed.

Run from the project folder: uv run python scripts/test_stations.py
"""

import asyncio
import math
import sys
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from nederland_mcp.bronnen import ns, ov

failed: list[str] = []


def check(name: str, ok: bool, got: Any = None) -> None:
    print(f"{'ok  ' if ok else 'FOUT'} {name}" + ("" if ok else f": {got}"))
    if not ok:
        failed.append(name)


def north(metres: float) -> float:
    """The latitude this many metres north of the made-up place at 52, 5."""
    return 52 + math.degrees(metres / 6_371_000)


def station(name: str, kind: str = "STOPTREIN_STATION", metres: float = 50_000, *synonyms: str,
            land: str = "NL", departures: bool = True) -> dict:
    """A station as NS lists it, this many metres north of the made-up place."""
    return {
        "code": "".join(word[0] for word in name.split()).upper() + str(len(name)), "namen": {"lang": name},
        "synoniemen": list(synonyms), "land": land,
        "stationType": kind, "heeftVertrektijden": departures, "lat": north(metres), "lng": 5.0,
    }


PLACE = {"lat": 52.0, "lon": 5.0}
STATIONS = [
    station("Gouda", "INTERCITY_STATION"),
    station("Gouda Goverwelle"),
    station("Hilversum", "KNOOPPUNT_INTERCITY_STATION"),
    station("Hilversum Media Park"),
    station("Hilversum Sportpark"),
    station("Almere Buiten", "SNELTREIN_STATION"),
    station("Almere Centrum", "KNOOPPUNT_INTERCITY_STATION"),
    station("Almere Muziekwijk"),
    station("Den Haag Centraal", "MEGA_STATION", 60_000, "Den Haag CS"),
    station("Den Haag HS", "KNOOPPUNT_INTERCITY_STATION", 60_000, "Den Haag Hollands Spoor"),
    station("Utrecht Centraal", "MEGA_STATION", 3700),
    station("Utrecht Lunetten", "STOPTREIN_STATION", 3000),
    station("Bunnik", "STOPTREIN_STATION", 3400),
    station("Utrecht Maliebaan", "STOPTREIN_STATION", 1000, "Spoorwegmuseum", departures=False),
    station("Zuidhorn"),
    station("Amsterdam Zuid", "KNOOPPUNT_INTERCITY_STATION"),
    station("Emmerich", "INTERCITY_STATION", 500, land="D"),
    station("Brussels Airport-Zaventem", "INTERCITY_STATION", land="B"),
    station("Brussel-Centraal", "MEGA_STATION", 50_000, "Bruxelles-Central", land="B"),
]


def match(name: str) -> str | None:
    found = ns._match(name, STATIONS)
    return ns._station_name(found) if found else None


def stop(name: str, metres: float = 0) -> dict:
    """A stop as Transitous finds it, with its town before the comma."""
    return {"name": name, "lat": north(metres), "lon": 5.0, "areas": []}


async def no_station(name: str, origin: Any, spot: Any = None, stations: list[dict] = STATIONS) -> str:
    """The error of the train tools for this name when Transitous gives this origin (or raises it) and this spot."""

    async def fake_origin(halte: str, plaats: str | None) -> Any:
        if isinstance(origin, Exception):
            raise origin
        return origin

    async def fake_spot(text: str, plaats: str | None) -> Any:
        if isinstance(spot, Exception):
            raise spot
        return spot

    async def fake_stations() -> list[dict]:
        return stations

    ov._origin, ov._spot, ns._stations = fake_origin, fake_spot, fake_stations
    try:
        await ns._station(name)
    except ToolError as error:
        return str(error)
    return "geen fout"


async def main() -> None:
    # Names as Haiku writes them.
    check("Gouda Centraal is Gouda", match("Gouda Centraal") == "Gouda", match("Gouda Centraal"))
    check("Station Hilversum is Hilversum, niet Media Park", match("Station Hilversum") == "Hilversum",
          match("Station Hilversum"))
    check("Hilversum station en Gouda CS ook",
          [match("Hilversum station"), match("Gouda CS")] == ["Hilversum", "Gouda"])
    check("Almere Centraal is het grootste station van Almere", match("Almere Centraal") == "Almere Centrum",
          match("Almere Centraal"))
    check("in het Engels: Utrecht Central", match("Utrecht Central") == "Utrecht Centraal", match("Utrecht Central"))
    check("over de grens alleen op de eigen naam: Brussels Central is niet Brussels Airport",
          [match("Brussels Central"), match("Brussel-Centraal"), match("Bruxelles Central")]
          == [None, "Brussel-Centraal", "Brussel-Centraal"],
          [match("Brussels Central"), match("Brussel-Centraal"), match("Bruxelles Central")])
    check("Station Zuid is niet Zuidhorn of Amsterdam Zuid", match("Station Zuid") is None, match("Station Zuid"))
    check("een gewone naam zoals altijd", [match("Den Haag Centraal"), match("Den Haag CS"), match("Gouda")]
          == ["Den Haag Centraal", "Den Haag Centraal", "Gouda"])
    check("een stuk van een naam: het grootste station dat zo begint", match("Den Haag") == "Den Haag Centraal",
          match("Den Haag"))

    # A place without a station.
    text = await no_station("LUMC", ({"lat": north(2820), "lon": 5.0, "naam": "LUMC Hoofdingang"}, None, [], True))
    check("om de hoek van een station: lopen", text == "'LUMC' is geen NS-station. Utrecht Lunetten ligt op 180 "
          "meter, ongeveer 3 minuten lopen: zoek de trein van of naar dat station.", text)
    text = await no_station("LUMC", ({"lat": north(2100), "lon": 5.0, "naam": "LUMC Hoofdingang"}, None, [], True))
    check("op 900 meter is het geen loopje meer", text.startswith("'LUMC' is geen NS-station. Het dichtstbijzijnde "
          "station is Utrecht Lunetten, op 0,9 km"), text)
    text = await no_station("Kantoor", ({"lat": north(3640), "lon": 5.0, "naam": "Kantoor"}, None, [], True))
    check("een station 60 meter verder", text == "'Kantoor' is geen NS-station. Utrecht Centraal ligt op 60 meter, "
          "ongeveer 1 minuut lopen: zoek de trein van of naar dat station.", text)
    text = await no_station("Utrecht Science Park", ({"lat": 52.0, "lon": 5.0, "naam": "Utrecht Science Park"}, None,
                                                     [], True))
    check("verder weg: het dichtstbijzijnde en het grootste station in de buurt, zonder het museumstation",
          text == "'Utrecht Science Park' is geen NS-station. Het dichtstbijzijnde station is Utrecht Lunetten, op "
          "3,0 km, en het grootste in de buurt Utrecht Centraal, op 3,7 km, met vaak de meeste bussen en trams. Zoek "
          "de trein van of naar een van die stations, en het stuk tussen het station en 'Utrecht Science Park' met "
          "ov_vertrek.", text)
    far = [s for s in STATIONS if s["namen"]["lang"] != "Utrecht Centraal"]
    far.append(station("Utrecht Centraal", "MEGA_STATION", 6100))
    text = await no_station("Utrecht Science Park", (PLACE, None, [], True), stations=far)
    check("het grootste station alleen als het niet veel verder is",
          "Utrecht Lunetten, op 3,0 km. Zoek de trein van of naar dat station," in text
          and "grootste" not in text, text)
    check("een station over de grens telt niet mee", "Emmerich" not in text, text)

    # What Transitous found for the name.
    beurs = stop("Rotterdam, Beurs", 3000)
    ahoy = {"lat": north(3300), "lon": 5.0, "naam": "Rotterdam Ahoy"}
    text = await no_station("Rotterdam Ahoy", (None, beurs, [beurs], False), ahoy)
    check("een halte die alleen de plaats deelt, telt niet: de plek zelf", "Bunnik ligt op 100 meter" in text, text)
    text = await no_station("Rotterdam Ahoy", (None, beurs, [beurs], False), None)
    check("zonder de plek zelf: geen stations, maar de algemene melding",
          text.startswith("Geen NS-station gevonden voor 'Rotterdam Ahoy'."), text)
    park = stop("Utrecht, P+R Science Park", 3640)
    text = await no_station("Utrecht Science Park", (None, park, [park], False), None)
    check("een halte met een eigen woord uit de naam telt wel", "Utrecht Centraal ligt op 60 meter" in text, text)
    text = await no_station("Utrecht Science Park", (None, park, [park], False),
                            ToolError("'Science Park' bestaat in meer dan één plaats"))
    check("ook als de plek in meer plaatsen bestaat", "Utrecht Centraal ligt op 60 meter" in text, text)
    text = await no_station("Nergenshuizen", ToolError("Geen halte gevonden"))
    check("niets gevonden: de algemene melding", text.startswith("Geen NS-station gevonden voor 'Nergenshuizen'."),
          text)
    found = await no_station("Gouda Centraal", ToolError("Transitous hoort niet gevraagd te worden"))
    check("een station vinden vraagt niets aan Transitous", found == "geen fout", found)

    # A station where trains seldom stop.
    maliebaan = next(s for s in STATIONS if s["namen"]["lang"] == "Utrecht Maliebaan")
    gouda = next(s for s in STATIONS if s["namen"]["lang"] == "Gouda")
    text = await ns._seldom([(gouda, "Gouda Centraal"), (maliebaan, "Spoorwegmuseum")])
    check("bij het museumstation de stations waar wel treinen stoppen, met de naam die gevraagd is",
          text.startswith(" Bij Utrecht Maliebaan stopt maar af en toe een trein. Het dichtstbijzijnde station is ")
          and "'Spoorwegmuseum' met ov_vertrek" in text, text)
    check("bij gewone stations niets erbij", await ns._seldom([(gouda, "Gouda")]) == "")

    print(f"\n{len(failed)} fout" if failed else "\nalles goed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

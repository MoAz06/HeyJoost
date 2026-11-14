"""Offline checks of how the train tools find a station by name, and what they say for a place without one, on
made-up NS stations and a made-up Transitous: no key and no network needed.

Run from the project folder: uv run python scripts/test_stations.py
"""

import asyncio
import math
import sys
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from nederland_mcp.bronnen import ns, ov, plaats

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


# The woonplaatsen PDOK knows here, and what Transitous was asked to find, as the place and its town.
TOWNS = {"Leiden", "Den Haag", "Utrecht", "Dorp"}
origins: list[tuple[str, str | None]] = []


def fake(
    origin: Any, spot: Any = None, town: Any = None, stations: list[dict] = STATIONS, places: dict | None = None
) -> None:
    """Transitous gives this origin (or raises it; per name and town when it is a dict), this spot and these towns and
    districts, PDOK this town, and NS these stations. Nothing goes to Transitous itself."""

    async def fake_origin(halte: str, plaats: str | None) -> Any:
        origins.append((halte, plaats))
        found = origin.get((halte, plaats), ToolError("Geen halte gevonden")) if isinstance(origin, dict) else origin
        if isinstance(found, Exception):
            raise found
        return found

    async def fake_spot(text: str, plaats: str | None) -> Any:
        if isinstance(spot, Exception):
            raise spot
        return spot

    async def fake_town(name: str) -> Any:
        if town is None:
            raise ToolError("Geen plaats gevonden")
        return town

    async def fake_stations() -> list[dict]:
        return stations

    async def fake_is_town(name: str) -> bool:
        return name in TOWNS

    async def fake_place(name: str) -> dict | None:
        return (places or {}).get(name)

    ov._origin, ov._spot, ov._place, plaats.zoek, plaats.is_town, ns._stations = (
        fake_origin, fake_spot, fake_place, fake_town, fake_is_town, fake_stations)


async def no_station(name: str, origin: Any, spot: Any = None, stations: list[dict] = STATIONS) -> str:
    """The error of the train tools for this name when Transitous gives this origin (or raises it) and this spot."""
    fake(origin, spot, stations=stations)
    try:
        await ns._station(name)
    except ToolError as error:
        return str(error)
    return "geen fout"


async def end(
    name: str, origin: Any, spot: Any = None, town: Any = None, places: dict | None = None
) -> tuple[str | None, dict | None] | str:
    """The station trein_reis takes for this end of a trip and the stretch to it, or the error."""
    fake(origin, spot, town, places=places)
    try:
        found, stretch = await ns._end(name, STATIONS)
    except ToolError as error:
        return str(error)
    return ns._station_name(found), stretch


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

    # Which station each end of a trip gets, and the stretch to it.
    found = await end("Gouda Centraal", ToolError("Transitous hoort niet gevraagd te worden"))
    check("A: een station is het eind zelf, zonder stuk lopen", found == ("Gouda", None), found)
    found = await end("LUMC", ({"lat": north(2820), "lon": 5.0, "naam": "LUMC Hoofdingang, Leiden"}, None, [], True))
    check("B: om de hoek van een station: het lopen uitgerekend, en Utrecht Centraal op 880 meter is te ver om te kiezen",
          found == ("Utrecht Lunetten", {"plek": "LUMC Hoofdingang, Leiden", "station": "Utrecht Lunetten", "meter": 180,
                                         "lopen_min": 3}), found)
    found = await end("LUMC", ({"lat": north(2100), "lon": 5.0, "naam": "LUMC Hoofdingang, Leiden"}, None, [], True))
    check("tot 1,5 km nog uitgerekend: 900 meter is 15 minuten lopen",
          found == ("Utrecht Lunetten", {"plek": "LUMC Hoofdingang, Leiden", "station": "Utrecht Lunetten", "meter": 900,
                                         "lopen_min": 15}), found)
    found = await end("Utrecht Science Park", (PLACE | {"naam": "Utrecht Science Park"}, None, [], True))
    check("C: verder weg het grootste station in de buurt, het stuk niet uitgerekend, wel 15 + 3 x 3,7 = 26, "
          "naar boven op 30 minuten speling",
          found == ("Utrecht Centraal", {"plek": "Utrecht Science Park", "station": "Utrecht Centraal", "meter": 3700,
                                         "speling_min": 30}), found)
    slack = [ns._slack(metres) for metres in (1570, 3660, 4400, 7340, 30_000)]
    check("speling: 20 minuten voor het Erasmus MC, 30 voor Science Park en Kurhaus, 40 voor 7,3 km, hooguit een uur",
          slack == [20, 30, 30, 40, 60], slack)
    found = await end("Dorp", ToolError("Geen halte gevonden"), None, plaats.Plaats("Dorp", north(3100), 5.0, "Dorp", "Utrecht"))
    check("een plaats zonder station: vanaf het midden, naar het dichtste van twee even grote stations",
          found == ("Utrecht Lunetten", {"plek": "Dorp", "station": "Utrecht Lunetten", "meter": 100, "lopen_min": 2}),
          found)
    dunes = plaats.Plaats("Wassenaar", north(6000), 5.0, "Wassenaar", "Zuid-Holland", "woonplaats")
    village = {"name": "Wassenaar", "lat": north(3100), "lon": 5.0, "category": "town", "areas": []}
    found = await end("Wassenaar", ToolError("Geen halte gevonden"), None, dunes, {"Wassenaar": village})
    check("een plaats zonder station: vanaf het dorp in OpenStreetMap, niet vanaf het midden van PDOK in de duinen",
          found == ("Utrecht Lunetten", {"plek": "Wassenaar", "station": "Utrecht Lunetten", "meter": 100, "lopen_min": 2}),
          found)
    found = await end("Wassenaar", ToolError("Geen halte gevonden"), None, dunes._replace(lat=north(3100)),
                      {"Wassenaar": village | {"lat": north(30_000)}})
    check("een plaats met die naam ver weg is een andere: dan het midden van PDOK",
          found == ("Utrecht Lunetten", {"plek": "Wassenaar", "station": "Utrecht Lunetten", "meter": 100, "lopen_min": 2}),
          found)
    kurhaus = stop("Den Haag, Kurhaus", 2900)
    district = {"name": "Scheveningen", "lat": north(2500), "lon": 5.0, "category": "suburb", "areas": []}
    found = await end("Kurhaus Scheveningen", {("Kurhaus", "Scheveningen"): (None, kurhaus, [kurhaus], True)}, None,
                      dunes._replace(naam="Scheveningen, Den Haag", lat=north(2000), soort="wijk"),
                      {"Scheveningen": district})
    check("met een wijk die geen woonplaats is: de halte in die wijk, zoals ov_vertrek hem vindt, niet het midden ervan",
          not isinstance(found, str) and found[1]["plek"] == "Den Haag, Kurhaus"
          and origins[-2:] == [("Kurhaus Scheveningen", None), ("Kurhaus", "Scheveningen")], (found, origins[-2:]))
    found = await end("Kerk", ({"lat": north(3500), "lon": 5.0, "naam": "Kerk, Utrecht"}, None, [], True))
    check("een groter station dat nauwelijks verder ligt: dat, zoals Leiden Centraal voor de Hooglandse Kerk",
          found == ("Utrecht Centraal", {"plek": "Kerk, Utrecht", "station": "Utrecht Centraal", "meter": 200,
                                         "lopen_min": 4}), found)
    found = await end("Binnenhof Den Haag", ({"lat": north(2820), "lon": 5.0, "naam": "Binnenhof, 's-Gravenhage"},
                                             None, [], True))
    check("Den Haag zoals iedereen het noemt", not isinstance(found, str) and found[1]["plek"] == "Binnenhof, Den Haag",
          found)
    hoofdingang = ({"lat": north(2820), "lon": 5.0, "naam": "LUMC Hoofdingang, Leiden"}, None, [], True)
    asked = []
    for name in ("LUMC Leiden", "Breestraat 70, Leiden", "Kurhaus Den Haag", "Utrecht Science Park", "LUMC"):
        await end(name, hoofdingang)
        asked.append(origins[-1])
    check("de plaats achteraan gaat apart mee, zoals ov_vertrek hem krijgt: het LUMC wordt rond Leiden gezocht",
          asked == [("LUMC", "Leiden"), ("Breestraat 70", "Leiden"), ("Kurhaus", "Den Haag"),
                    ("Utrecht Science Park", None), ("LUMC", None)], asked)
    elsewhere = stop("Zoetermeer, Stadhuis", 2820) | {"areas": [{"name": "Zoetermeer", "adminLevel": 8}]}
    found = await end("stadhuis Leiden", (None, elsewhere, [elsewhere], True), None,
                      plaats.Plaats("Leiden", north(3300), 5.0, "Leiden", "Zuid-Holland"))
    check("een halte in een andere plaats dan genoemd is het niet: Zoetermeer Stadhuis voor 'stadhuis Leiden'",
          not isinstance(found, str) and found[1]["plek"] == "Leiden", found)
    found = await end("Kerkstraat 1", ov.Ambiguous("'Kerkstraat 1' bestaat in meer dan één plaats, bijvoorbeeld: Zwolle."))
    check("een adres in meer plaatsen: die vraag gaat door", isinstance(found, str) and "meer dan één plaats" in found, found)
    found = await end("Nergenshuizen", ToolError("Geen halte gevonden"))
    check("niets gevonden: noem een station, adres of plek", isinstance(found, str)
          and found.startswith("Geen station, adres of plek gevonden voor 'Nergenshuizen'."), found)
    found = await end("Adres 1, 1234 AB Dorp", ({"lat": north(2820), "lon": 5.0, "naam": "Adres 1, 1234AB Dorp"}, None, [], True))
    check("de naam van een adres zonder postcode", not isinstance(found, str) and found[1]["plek"] == "Adres 1, Dorp", found)

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

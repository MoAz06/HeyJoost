"""Door to door by public transport: from an address, street, station or place to another, with walking, bus, tram,
metro, train and ferry, from the journey planner of Transitous (MOTIS).

Transitous asks projects to contact them before using routing, which costs them far more than departures. So this
tool stays off until they agree: it only exists when HEYJOOST_REISPLANNER=1, and then the whole service asks at most
HEYJOOST_REISPLANNER_PER_DAG plans a day (200 if not set). As with the departures, Transitous only gets points rounded
to about a hundred metres, never an address. A stop or a place like the Kurhaus goes as the stop itself.
"""

import os
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, fmt_time, get_json, now_local, parse_moment, parse_time
from . import ns
from . import plaats as plaatsen
from .ov import BASE, SOURCE, _address, _distance, _find, _is_address, _near, _spot
from .plaats import common_name

AAN = os.environ.get("HEYJOOST_REISPLANNER") == "1"
BRON = (
    "Reisplanner van Transitous (https://transitous.org/sources/), met de dienstregeling en live-data van de "
    "vervoerders. Looproutes © OpenStreetMap-bijdragers."
)
MODES = {
    "WALK": "lopen", "BUS": "bus", "COACH": "bus", "TRAM": "tram", "SUBWAY": "metro", "METRO": "metro",
    "FERRY": "veerboot", "RAIL": "trein", "HIGHSPEED_RAIL": "trein", "LONG_DISTANCE": "trein", "NIGHT_RAIL": "trein",
    "REGIONAL_RAIL": "trein", "REGIONAL_FAST_RAIL": "trein", "SUBURBAN": "trein",
}
MAX_TRIPS = 3
# Transitous was told: at most two plans per question in the chat, the second chance in reis_plannen included.
PER_QUESTION = 2
# The plan requests of the whole service today, so a busy day stays light for Transitous.
_today: dict[str, Any] = {"dag": None, "aantal": 0}
# The plans the current chat question may still ask. A list, so the lookups that run side by side share it. None
# outside the chat: an MCP client only has the cap of the day.
_left: ContextVar[list[int] | None] = ContextVar("reisplanner_left", default=None)


def new_question() -> None:
    """A chat question starts: from here it may ask PER_QUESTION plans."""
    _left.set([PER_QUESTION])


def _per_day() -> int:
    return int(os.environ.get("HEYJOOST_REISPLANNER_PER_DAG") or 200)


class _Place:
    """Where a trip starts or ends: what the planner gets, the name for the answer, and for an address its point."""

    def __init__(self, place: str, name: str, point: dict | None = None) -> None:
        self.place, self.name, self.point = place, name, point


async def _point(text: str, plaats: str | None) -> _Place:
    text = text.strip()
    both = " ".join(part for part in (text, plaats) if part)
    if plaats or _is_address(text):
        found = await _address(text, plaats)
        if found:
            # The street and town, without the house number that was asked.
            name = re.sub(r"\s+\d+\S*(?=,)|\s*\b\d{4}\s?[A-Z]{2}\b", "", found["naam"])
            return _Place(f"{found['lat']:.3f},{found['lon']:.3f}", common_name(name), found)
    if _is_address(text):
        raise ToolError(f"Geen adres gevonden dat lijkt op '{text}'. Noem de straat met huisnummer en de plaats.")
    station = await _station(text, plaats)
    if station:
        # A station is a public place, so its point needs no rounding.
        return _Place(f"{station['lat']:.5f},{station['lng']:.5f}", ns._station_name(station))
    # A stop or a spot with its own stop, like the Kurhaus: the stop itself, so the trip starts there and not on the
    # street outside.
    try:
        stop, _, sure = await _find(text, plaats)
    except ToolError:
        stop, sure = None, False
    if stop and sure:
        return _Place(stop["id"], common_name(stop["name"]))
    # A place that no stop is named after, like the LUMC or Rotterdam Ahoy, or that only lends its name to a stop,
    # like the Erasmus MC: the place itself, as ov_vertrek finds it. Without this the trip went to the middle of the
    # town. It is a public place too, so no rounding.
    spot = await _spot(text, plaats)
    if spot:
        return _Place(f"{spot['lat']:.5f},{spot['lon']:.5f}", common_name(spot["naam"]), spot)
    try:
        place = await plaatsen.zoek(both)
    except ToolError:
        raise ToolError(
            f"Geen plek gevonden die lijkt op '{text}'. Noem een adres, straat, station, halte of plaats."
        ) from None
    return _Place(f"{place.lat:.3f},{place.lon:.3f}", place.naam)


async def _station(text: str, plaats: str | None) -> dict | None:
    """The NS station meant, the same one trein_reis takes: Utrecht is Utrecht Centraal, where Transitous gave a spot
    in Bunnik called Utrecht. With a town, the text alone counts as well when the station is in that town, as Leiden
    Centraal with Leiden and Science Park with Amsterdam, but not Science Park with Utrecht. None when no station has
    that name."""
    try:
        stations = await ns._stations()
    except ToolError:
        return None
    found = ns._match(f"{text} {plaats}" if plaats else text, stations)
    if found is None and plaats:
        # Whole words, so Ede is not Rheden.
        town = f" {' '.join(ns._words(plaats))} "
        found = ns._match(text, [s for s in stations if any(town in f" {' '.join(ns._words(n))} " for n in ns._names(s))])
    return found if found and found.get("lat") and found.get("lng") else None


async def _stop_near(place: _Place) -> tuple[_Place, int] | None:
    """The stop nearest to an address, and how far it is, for when the rounded point has no way to the stops: it can
    fall in a pond or a closed courtyard."""
    stops = await _near(place.point) if place.point else []
    if not stops:
        return None
    return _Place(stops[0]["id"], common_name(stops[0]["name"])), round(_distance(place.point, stops[0]) / 50) * 50


def _leg(leg: dict, names: dict[str, str]) -> dict[str, Any]:
    start, end = leg.get("from") or {}, leg.get("to") or {}
    kind = MODES.get(leg.get("mode") or "", (leg.get("mode") or "").lower())
    planned, actual = parse_time(start.get("scheduledDeparture")), parse_time(start.get("departure"))
    # The planner calls the two ends START and END.
    there = names.get(end.get("name") or "", common_name(end.get("name") or ""))
    part: dict[str, Any] = {"soort": kind}
    if kind == "lopen":
        part["minuten"] = round((leg.get("duration") or 0) / 60)
        if leg.get("distance"):
            part["meter"] = round(leg["distance"] / 50) * 50
        part["naar"] = there
        return {key: value for key, value in part.items() if value}
    part.update(
        lijn=leg.get("routeShortName") or leg.get("displayName"),
        richting=leg.get("headsign"),
        van=names.get(start.get("name") or "", common_name(start.get("name") or "")),
        vertrek=fmt_time(planned),
        spoor_of_perron=start.get("track") or start.get("scheduledTrack"),
        naar=there,
        aankomst=fmt_time(parse_time(end.get("scheduledArrival"))),
        vervoerder=leg.get("agencyName"),
    )
    if leg.get("realTime") and planned and actual:
        delay = round((actual - planned).total_seconds() / 60)
        if delay:
            part["vertraging_min"] = delay
    return {key: value for key, value in part.items() if value}


async def _ask(params: dict) -> dict:
    """One plan from Transitous, counted for the day and for the chat question. A plan from the cache is not asked
    and does not count."""
    day = now_local().date()
    if _today["dag"] != day:
        _today.update(dag=day, aantal=0)
    if _today["aantal"] >= _per_day():
        raise ToolError(
            "De reisplanner zit voor vandaag aan zijn maximum. Plan de reis in stukken met trein_reis en ov_vertrek, "
            "of probeer het morgen weer."
        )
    left = _left.get()
    if left is not None and left[0] <= 0:
        raise ToolError(
            "Voor deze vraag zijn al genoeg reizen gepland. Antwoord met wat je al hebt, of plan de rest in stukken "
            "met trein_reis en ov_vertrek."
        )
    _today["aantal"] += 1
    if left is not None:
        left[0] -= 1
    return await get_json(f"{BASE}/v6/plan", params=params, source=SOURCE)


async def _plan(start: str, end: str, moment: datetime, aankomst: bool) -> list[dict]:
    params = {
        "fromPlace": start, "toPlace": end, "time": moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "arriveBy": "true" if aankomst else "false", "numItineraries": MAX_TRIPS, "detailedTransfers": "false",
        "language": "nl",
    }
    data = await cached(("reisplanner", tuple(sorted(params.items()))), 60, lambda: _ask(params))
    return data.get("itineraries") or []


async def reis_plannen(
    van: str, naar: str, van_plaats: str | None = None, naar_plaats: str | None = None,
    datum: str | None = None, tijd: str | None = None, aankomst: bool = False,
) -> dict[str, Any]:
    start, end = await _point(van, van_plaats), await _point(naar, naar_plaats)
    if start.place == end.place:
        raise ToolError("Begin en eind liggen op dezelfde plek. Noem een ander adres of een andere plaats.")
    moment = parse_moment(datum, tijd)
    names = {"START": start.name, "END": end.name}
    found = await _plan(start.place, end.place, moment, aankomst)
    notes = []
    if not found and (start.point or end.point):
        # Once more from the stops nearest to the addresses. That walk is not in the trip, so it is said apart.
        near_start, near_end = await _stop_near(start), await _stop_near(end)
        if near_start:
            notes.append(f"Loop eerst naar de halte {near_start[0].name}, ongeveer {near_start[1]} meter hemelsbreed.")
            start = near_start[0]
        if near_end:
            notes.append(f"Loop vanaf de halte {near_end[0].name} nog ongeveer {near_end[1]} meter hemelsbreed.")
            end = near_end[0]
        if near_start or near_end:
            found = await _plan(start.place, end.place, moment, aankomst)
    # Quickest there first, or for an arrival time the one that leaves last.
    found = sorted(found, key=lambda trip: (trip.get("endTime") or "", trip.get("duration") or 0))
    if aankomst:
        found.sort(key=lambda trip: trip.get("startTime") or "", reverse=True)
    trips = [
        {
            "vertrek": fmt_time(parse_time(trip.get("startTime"))),
            "aankomst": fmt_time(parse_time(trip.get("endTime"))),
            "reistijd_min": round((trip.get("duration") or 0) / 60),
            "overstappen": trip.get("transfers"),
            "delen": [_leg(leg, names) for leg in trip.get("legs") or []],
        }
        for trip in found[:MAX_TRIPS]
    ]
    if not trips:
        raise ToolError(f"Geen reis gevonden van {names['START']} naar {names['END']}. Probeer een ander tijdstip.")
    result: dict[str, Any] = {"van": names["START"], "naar": names["END"], "reizen": trips}
    if notes:
        result["let_op"] = " ".join(notes)
    result["bron"] = BRON
    return result

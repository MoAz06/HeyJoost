"""Door to door by public transport: from an address, street, station or place to another, with walking, bus, tram,
metro, train and ferry, from the journey planner of Transitous (MOTIS).

trein_reis asks it for a place too far from a station to walk (see ns._end), so the code decides and not Haiku. The
trips come in the shape of trein_reis, and the chat shows them as cards.

Transitous asks projects to contact them before using routing, which costs them far more than departures. They agreed,
and it is on when HEYJOOST_REISPLANNER=1; then the whole service asks at most HEYJOOST_REISPLANNER_PER_DAG plans a day
(200 if not set). As with the departures, Transitous only gets points rounded to about a hundred metres, never an
address. A stop or a place like the Kurhaus goes as the stop itself.
"""

import asyncio
import logging
import os
import re
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, fmt_time, get_json, now_local, parse_moment, parse_time
from . import ns
from . import plaats as plaatsen
from .ov import BASE, SOURCE, _address, _district, _find, _is_address, _middle, _spot
from .plaats import common_name

log = logging.getLogger(__name__)
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
# Transitous was told: at most two plans per question in the chat.
PER_QUESTION = 2
# Minutes the planner keeps on top of each transfer: without them the best trip from Gouda to the Science Park had two
# minutes from the train to the bus at Utrecht Centraal, the walk included.
EXTRA_TRANSFER = 2
# For someone who will not pay a supplement: every mode but the fast trains. Transitous files the Intercity direct and
# the Eurocity Direct under LONG_DISTANCE and the Eurostar under HIGHSPEED_RAIL, and the Intercity and the Sprinter under
# REGIONAL_RAIL (seen at Rotterdam Centraal). Without this all three trips from Amsterdam to the Erasmus MC went by
# Intercity direct or Eurocity Direct.
NO_SUPPLEMENT = "TRAM,SUBWAY,FERRY,BUS,COACH,REGIONAL_FAST_RAIL,REGIONAL_RAIL,SUBURBAN,NIGHT_RAIL"
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
    found = await _named(text, plaats)
    if found is None and not plaats:
        # A district at the end that is no woonplaats, as Scheveningen in 'Kurhaus Scheveningen': the stop or place in
        # it. Without this the trip went to the middle of Scheveningen, a kilometre from the Kurhaus.
        parts = await _district(text)
        found = await _named(*parts) if parts else None
    if found:
        return found
    try:
        place = await plaatsen.zoek(both)
    except ToolError:
        raise ToolError(
            f"Geen plek gevonden die lijkt op '{text}'. Noem een adres, straat, station, halte of plaats."
        ) from None
    # A town without a station from its old centre, where its stops are, and not from the middle of its area.
    middle = await _middle(place)
    return _Place(f"{middle['lat']:.3f},{middle['lon']:.3f}", place.naam)


async def _named(text: str, plaats: str | None) -> _Place | None:
    """A stop or a place with this name, or None."""
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
    return None


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


def _kind(leg: dict) -> str:
    return MODES.get(leg.get("mode") or "", (leg.get("mode") or "").lower())


def _part(leg: dict, names: dict[str, str], transfer: bool) -> dict[str, Any]:
    """A leg of a plan as a part of a trip in trein_reis (see ns._leg): a walk, a train, or a bus, tram, metro or ferry
    with its line and stop. A walk between two rides is the transfer, and says so."""
    start, end = leg.get("from") or {}, leg.get("to") or {}
    # The planner calls the two ends START and END.
    here = names.get(start.get("name") or "", common_name(start.get("name") or ""))
    there = names.get(end.get("name") or "", common_name(end.get("name") or ""))
    kind = _kind(leg)
    if kind == "lopen":
        return {"lopen_min": max(1, round((leg.get("duration") or 0) / 60)), "van": here,
                "vertrek": fmt_time(parse_time(leg.get("startTime"))), "naar": there,
                "aankomst": fmt_time(parse_time(leg.get("endTime"))), **({"overstap": True} if transfer else {})}
    planned, actual = parse_time(leg.get("scheduledStartTime")), parse_time(leg.get("startTime"))
    late = round((actual - planned).total_seconds() / 60) if leg.get("realTime") and planned and actual else 0
    track = start.get("track") or start.get("scheduledTrack")
    if kind == "trein":
        ride: dict[str, Any] = {"trein": leg.get("displayName") or leg.get("routeShortName") or "Trein", "spoor": track}
    else:
        ride = {"soort": kind, "lijn": leg.get("routeShortName") or leg.get("displayName"), "perron": track}
    return {
        **ride,
        "richting": leg.get("headsign"),
        "van": here,
        # As for NS: the departure as planned with the delay apart, and the arrival as expected now.
        "vertrek": fmt_time(planned),
        "vertraging_min": late,
        "naar": there,
        "aankomst": fmt_time(parse_time(leg.get("endTime"))),
        **({"vervoerder": leg["agencyName"]} if leg.get("agencyName") else {}),
        **({"geannuleerd": True} if leg.get("cancelled") else {}),
    }


def _gaps(trip: dict) -> list[tuple[int, int]]:
    """The transfers of a trip: the leg of the ride before each, and the minutes from that ride to the next with the
    walk in between, which the planner already made EXTRA_TRANSFER longer."""
    legs = trip.get("legs") or []
    rides = [index for index, leg in enumerate(legs) if _kind(leg) != "lopen"]
    gaps = []
    for before, after in zip(rides, rides[1:]):
        arrived, leaves = parse_time(legs[before].get("endTime")), parse_time(legs[after].get("startTime"))
        if arrived and leaves:
            gaps.append((before, round((leaves - arrived).total_seconds() / 60)))
    return gaps


def _train(leg: dict) -> tuple | None:
    """A train that may cost a supplement, as NS is asked about it: from, to, the time it leaves and its number."""
    leaves = parse_time(leg.get("scheduledStartTime") or leg.get("startTime"))
    name = leg.get("displayName") or leg.get("routeShortName") or ""
    if _kind(leg) != "trein" or not leaves or not ns.SUPPLEMENT.match(name):
        return None
    start, end = leg.get("from") or {}, leg.get("to") or {}
    return start.get("name") or "", end.get("name") or "", leaves, leg.get("tripShortName")


async def _supplements(trips: list[dict]) -> dict[tuple, float]:
    """The supplements NS asks for the trains of these trips, by train (see _train). Each train is asked once, and
    when NS cannot tell, the trip goes without, as before."""
    trains = {train for trip in trips for leg in trip.get("legs") or [] if (train := _train(leg))}

    async def ask(train: tuple) -> float | None:
        try:
            return await ns._supplement(*train)
        except Exception as exc:
            log.info("reisplanner: geen toeslag van NS (%s)", type(exc).__name__)
            return None

    amounts = await asyncio.gather(*(ask(train) for train in trains))
    return {train: amount for train, amount in zip(trains, amounts) if amount}


def _advice(trip: dict, names: dict[str, str], supplements: dict[tuple, float] | None = None) -> dict[str, Any]:
    """A trip of a plan as trein_reis gives one, with times from door to door. The minutes to change are from one ride
    to the next with the walk in between, and a supplement is what NS asks for its trains (see _supplements)."""
    legs = trip.get("legs") or []
    rides = [index for index, leg in enumerate(legs) if _kind(leg) != "lopen"]
    parts = [_part(leg, names, bool(rides) and rides[0] < index < rides[-1]) for index, leg in enumerate(legs)]
    changes = [ns._change_line(parts[before]["naar"] or "?", gap) for before, gap in _gaps(trip)]
    extra = sum((supplements or {}).get(_train(leg), 0) for leg in legs if _train(leg))
    # From a station the planner first walks from its point to the platform, "from Gouda to Gouda": the station is
    # the start itself, as in the trips of NS.
    keep = [
        index for index, part in enumerate(parts)
        if not ("lopen_min" in part and index in (0, len(parts) - 1)
                and ns._words(part.get("van") or "") == ns._words(part.get("naar") or ""))
    ] or list(range(len(parts)))
    begins, ends = parse_time(legs[keep[0]].get("startTime")), parse_time(legs[keep[-1]].get("endTime"))
    return {
        "vertrek": fmt_time(begins),
        "aankomst": fmt_time(ends),
        "reistijd_min": round((ends - begins).total_seconds() / 60) if begins and ends else None,
        "overstappen": trip.get("transfers") or 0,
        **({"overstaptijd": changes} if changes else {}),
        # As NS gives it: for the Intercity direct, ICE or Eurostar you pay this on top of your ticket.
        **({"toeslag_euro": round(extra, 2)} if extra else {}),
        "delen": [parts[index] for index in keep],
    }


async def _ask(params: dict) -> dict:
    """One plan from Transitous, counted for the day and for the chat question. A plan from the cache is not asked
    and does not count."""
    day = now_local().date()
    if _today["dag"] != day:
        _today.update(dag=day, aantal=0)
    if _today["aantal"] >= _per_day():
        raise ToolError("De reisplanner zit voor vandaag aan zijn maximum.")
    left = _left.get()
    if left is not None and left[0] <= 0:
        raise ToolError("Voor deze vraag zijn al genoeg reizen gepland.")
    _today["aantal"] += 1
    if left is not None:
        left[0] -= 1
    return await get_json(f"{BASE}/v6/plan", params=params, source=SOURCE)


async def _plan(start: str, end: str, moment: datetime, aankomst: bool, zonder_toeslag: bool = False) -> list[dict]:
    params = {
        "fromPlace": start, "toPlace": end, "time": moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "arriveBy": "true" if aankomst else "false", "numItineraries": MAX_TRIPS, "detailedTransfers": "false",
        "additionalTransferTime": EXTRA_TRANSFER, "language": "nl",
        **({"transitModes": NO_SUPPLEMENT} if zonder_toeslag else {}),
    }
    data = await cached(("reisplanner", tuple(sorted(params.items()))), 60, lambda: _ask(params))
    return data.get("itineraries") or []


async def deur_tot_deur(
    van: str, naar: str, datum: str | None, tijd: str | None, aankomst: bool, zonder_toeslag: bool = False,
) -> dict[str, Any]:
    """trein_reis for a place too far from a station to walk, or for two places at the same station: the trip from
    door to door with one plan, in the shape of trein_reis. For an arrival ns.MARGIN comes off the asked time, as for a
    train, and with zonder_toeslag the plan leaves out the trains that cost a supplement. A ToolError when the planner
    may not ask or finds nothing, so trein_reis can take the train instead."""
    start, end = await _point(*await ns._split(van)), await _point(*await ns._split(naar))
    if start.place == end.place:
        raise ToolError("Begin en eind liggen op dezelfde plek.")
    asked = parse_moment(datum, tijd)
    moment = asked - timedelta(minutes=ns.MARGIN) if aankomst else asked
    names = {"START": start.name, "END": end.name}

    def fits(trip: dict) -> bool:
        begins, ends = parse_time(trip.get("startTime")), parse_time(trip.get("endTime"))
        if not begins or not ends:
            return False
        return ends <= moment if aankomst else begins >= moment - timedelta(minutes=1)

    plan = await _plan(start.place, end.place, moment, aankomst, zonder_toeslag)
    found = [trip for trip in plan if fits(trip) and trip.get("legs")]
    if not found:
        raise ToolError(f"De reisplanner vond geen reis van {start.name} naar {end.name}.")

    def order(trip: dict) -> tuple:
        """As ns._pick weighs NS trips: the first there, or for an arrival time the last to leave, where each transfer
        counts as ns.TRANSFER_MINUTES and a tight one twice. The planner's best to the Erasmus MC had three transfers,
        two of them tight, to be there two minutes sooner."""
        begins, ends = (parse_time(trip[key]).timestamp() / 60 for key in ("startTime", "endTime"))
        gaps = [gap for _, gap in _gaps(trip)]
        weight = ns.TRANSFER_MINUTES * (len(gaps) + sum(gap < ns.TIGHT for gap in gaps))
        if aankomst:
            return weight - begins + ns.OTHER_END * ends, len(gaps), ends
        return ends + weight - ns.OTHER_END * begins, len(gaps), -begins

    best = min(found, key=order)
    # The others in time order, as for NS.
    others = sorted((trip for trip in found if trip is not best), key=lambda trip: trip["startTime"])
    chosen = [best, *others][:MAX_TRIPS]
    supplements = await _supplements(chosen)
    trips = [_advice(trip, names, supplements) for trip in chosen]
    trips[0] = {"beste": True, **trips[0]}
    kind = "aankomst" if aankomst else "vertrek"
    result: dict[str, Any] = {
        "van": start.name,
        "naar": end.name,
        # Planned with bus, tram or metro, so there is no NS price or crowd forecast.
        "reisplanner": True,
        "gezocht_op": f"{kind} {moment.strftime('%Y-%m-%d %H:%M')}",
        "gevraagd": f"{kind} {asked.strftime('%Y-%m-%d %H:%M')}",
        "dag": ns._day(moment),
        **({"dienstregeling": ns._timetable(moment.date())} if moment.date() > now_local().date() else {}),
    }
    if aankomst:
        result["op_tijd_voor"] = f"{fmt_time(asked)}, met {ns.MARGIN} minuten marge"
    result["reisadviezen"] = trips
    return {**result, "bron": BRON}


async def reis_plannen(
    van: str, naar: str, van_plaats: str | None = None, naar_plaats: str | None = None,
    datum: str | None = None, tijd: str | None = None, aankomst: bool = False, zonder_toeslag: bool = False,
) -> dict[str, Any]:
    """For MCP clients: the same as trein_reis, which takes the train where it can and the planner where it must."""
    def joined(text: str, town: str | None) -> str:
        return f"{text} {town}" if town and town.lower() not in text.lower() else text

    return await ns.trein_reis(
        joined(van, van_plaats), joined(naar, naar_plaats), datum, tijd, aankomst, zonder_toeslag=zonder_toeslag,
    )

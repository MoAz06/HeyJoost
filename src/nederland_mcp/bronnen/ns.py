"""NS Reisinformatie API: stations, departures, arrivals and journey planning.

Needs NS_API_KEY (a subscription key from apiportal.ns.nl).
"""

import os
import re
from datetime import date, datetime
from datetime import time as dt_time
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from ..common import AMSTERDAM, cached, fmt_time, get_json, normalize, now_local, parse_time

BASE = "https://gateway.apiportal.ns.nl/reisinformatie-api/api"
SOURCE = "NS Reisinformatie"
BRON = "NS Reisinformatie API"
CROWD = {"LOW": "rustig", "MEDIUM": "gemiddeld", "HIGH": "druk"}
BOARDS = {"departures": "vertrektijden", "arrivals": "aankomsttijden"}


def _headers() -> dict:
    key = os.environ.get("NS_API_KEY")
    if not key:
        raise ToolError("Treininformatie is nog niet beschikbaar: de server heeft geen NS API-key (NS_API_KEY).")
    return {"Ocp-Apim-Subscription-Key": key}


async def _stations() -> list[dict]:
    data = await cached(
        ("ns-stations",),
        24 * 3600,
        lambda: get_json(f"{BASE}/v2/stations", headers=_headers(), source=SOURCE),
    )
    return data.get("payload", []) if isinstance(data, dict) else []


async def _station(name: str) -> dict:
    stations = await _stations()
    q = normalize(name)

    def names(station: dict) -> list[str]:
        namen = station.get("namen") or {}
        return [station.get("code", ""), *namen.values(), *(station.get("synoniemen") or [])]

    def rank(station: dict) -> int:
        normalized = [normalize(n) for n in names(station) if n]
        if q in normalized:
            return 0
        if any(n.startswith(q) for n in normalized):
            return 1
        if any(q in n for n in normalized):
            return 2
        return 3

    ranked = sorted((s for s in stations if rank(s) < 3), key=lambda s: (rank(s), s.get("land") != "NL"))
    if not ranked:
        raise ToolError(f"Geen NS-station gevonden voor '{name}'.")
    return ranked[0]


def _station_name(station: dict) -> str:
    return (station.get("namen") or {}).get("lang") or station.get("code", "")


def _delay_minutes(planned: datetime | None, actual: datetime | None) -> int:
    if not planned or not actual:
        return 0
    return max(0, round((actual - planned).total_seconds() / 60))


async def _board(kind: str, station: str, aantal: int, datum: str | None, tijd: str | None) -> tuple[dict, list[dict]]:
    found = await _station(station)
    aantal = max(1, min(aantal, 25))
    params = {"station": found.get("code"), "maxJourneys": aantal}
    moment = _moment(datum, tijd) if datum or tijd else None
    if moment:
        params["dateTime"] = moment.isoformat(timespec="seconds")
    data = await cached(
        ("ns-" + kind, tuple(params.items())),
        30,
        lambda: get_json(f"{BASE}/v2/{kind}", params=params, headers=_headers(), source=SOURCE),
    )
    items = (data.get("payload") or {}).get(kind, [])[:aantal]
    if moment and not items:
        # The boards only reach about an hour ahead; later moments come back empty instead of failing.
        raise ToolError(
            f"NS heeft geen {BOARDS[kind]} voor {_station_name(found)} vanaf {moment.strftime('%Y-%m-%d %H:%M')}. "
            "Deze lijst gaat maar ongeveer een uur vooruit. Gebruik voor een later moment trein_reis met die tijd."
        )
    return found, items


def _board_item(item: dict, time_key: str, place_key: str, place: str | None) -> dict[str, Any]:
    planned, actual = parse_time(item.get("plannedDateTime")), parse_time(item.get("actualDateTime"))
    planned_track, actual_track = item.get("plannedTrack"), item.get("actualTrack")
    product = item.get("product") or {}
    return {
        time_key: fmt_time(planned),
        "vertraging_min": _delay_minutes(planned, actual),
        place_key: place,
        "trein": product.get("longCategoryName") or item.get("trainCategory"),
        "treinnummer": product.get("number"),
        "vervoerder": product.get("operatorName"),
        "spoor": actual_track or planned_track,
        "spoorwijziging": bool(actual_track and planned_track and actual_track != planned_track),
        "geannuleerd": bool(item.get("cancelled")),
        "meldingen": [m.get("message") for m in (item.get("messages") or []) if m.get("message")],
    }


async def trein_vertrek(station: str, aantal: int = 10, datum: str | None = None, tijd: str | None = None) -> dict:
    found, items = await _board("departures", station, aantal, datum, tijd)
    departures = [
        {
            **_board_item(item, "vertrek", "richting", item.get("direction")),
            "via": [s.get("mediumName") for s in (item.get("routeStations") or [])[:3]],
        }
        for item in items
    ]
    return {"station": _station_name(found), "vertrektijden": departures, "bron": BRON}


async def trein_aankomst(station: str, aantal: int = 10, datum: str | None = None, tijd: str | None = None) -> dict:
    found, items = await _board("arrivals", station, aantal, datum, tijd)
    arrivals = [_board_item(item, "aankomst", "herkomst", item.get("origin")) for item in items]
    return {"station": _station_name(found), "aankomsttijden": arrivals, "bron": BRON}


def _moment(datum: str | None, tijd: str | None) -> datetime:
    now = now_local()
    try:
        day = date.fromisoformat(datum.strip()) if datum else now.date()
        if tijd:
            hour, minute = map(int, re.fullmatch(r"(\d{1,2})[:.](\d{2})", tijd.strip()).groups())
            at = dt_time(hour, minute)
        else:
            at = dt_time(now.hour, now.minute)
    except (ValueError, AttributeError) as exc:
        raise ToolError("Gebruik datum als JJJJ-MM-DD en tijd als UU:MM, bijvoorbeeld 2026-10-12 en 08:30.") from exc
    return datetime.combine(day, at, tzinfo=AMSTERDAM)


async def trein_reis(van: str, naar: str, datum: str | None = None, tijd: str | None = None, aankomst: bool = False) -> dict:
    origin, destination = await _station(van), await _station(naar)
    moment = _moment(datum, tijd)

    params = {
        "fromStation": origin.get("code"),
        "toStation": destination.get("code"),
        "dateTime": moment.isoformat(timespec="seconds"),
        "searchForArrival": str(aankomst).lower(),
    }
    async def search(extra: dict | None = None) -> dict:
        query = {**params, **(extra or {})}
        return await cached(
            ("ns-reis", tuple(query.items())),
            60,
            lambda: get_json(f"{BASE}/v3/trips", params=query, headers=_headers(), source=SOURCE),
        )

    def fits(option: tuple) -> bool:
        return option[1] <= moment if aankomst else option[0] >= moment

    data = await search()
    options = _options(data.get("trips") or [])
    backward = data.get("scrollRequestBackwardContext")
    if aankomst and backward and not any(map(fits, options)):
        # NS starts an arrival search at the first trip arriving after the asked time; earlier ones are on the previous page.
        earlier = await search({"context": backward})
        options = _options(earlier.get("trips") or []) + options
    # NS also returns options just outside the asked time, such as a train that already left.
    fitting = [o for o in options if fits(o)]
    chosen = (fitting[-5:] if aankomst else fitting[:5]) or options[:5]

    trips = []
    for departure, arrival, trip, legs in chosen:
        fare = trip.get("productFare") or {}
        trips.append({
            "vertrek": fmt_time(departure),
            "aankomst": fmt_time(arrival),
            "reistijd_min": trip.get("actualDurationInMinutes") or trip.get("plannedDurationInMinutes"),
            "overstappen": trip.get("transfers"),
            "status": trip.get("status"),
            "drukte": CROWD.get(trip.get("crowdForecast"), trip.get("crowdForecast")),
            "prijs_euro": fare["priceInCents"] / 100 if fare.get("priceInCents") else None,
            "delen": [_leg(leg) for leg in legs],
            "link": (trip.get("shareUrl") or {}).get("uri"),
        })
    if not trips:
        raise ToolError(f"Geen reisadvies gevonden van {_station_name(origin)} naar {_station_name(destination)}.")
    return {
        "van": _station_name(origin),
        "naar": _station_name(destination),
        "gezocht_op": f"{'aankomst' if aankomst else 'vertrek'} {moment.strftime('%Y-%m-%d %H:%M')}",
        "reisadviezen": trips,
        "bron": BRON,
    }


def _options(trips: list[dict]) -> list[tuple[datetime, datetime, dict, list[dict]]]:
    options = []
    for trip in trips:
        legs = trip.get("legs") or []
        if not legs:
            continue
        first, last = legs[0].get("origin") or {}, legs[-1].get("destination") or {}
        departure = parse_time(first.get("actualDateTime") or first.get("plannedDateTime"))
        arrival = parse_time(last.get("actualDateTime") or last.get("plannedDateTime"))
        if departure and arrival:
            options.append((departure, arrival, trip, legs))
    return options


def _leg(leg: dict) -> dict[str, Any]:
    origin, destination = leg.get("origin") or {}, leg.get("destination") or {}
    product = leg.get("product") or {}
    planned = parse_time(origin.get("plannedDateTime"))
    actual = parse_time(origin.get("actualDateTime"))
    return {
        "trein": product.get("displayName") or product.get("shortCategoryName") or leg.get("name"),
        "richting": leg.get("direction"),
        "van": origin.get("name"),
        "vertrek": fmt_time(planned),
        "vertraging_min": _delay_minutes(planned, actual),
        "spoor": origin.get("actualTrack") or origin.get("plannedTrack"),
        "naar": destination.get("name"),
        "aankomst": fmt_time(parse_time(destination.get("actualDateTime") or destination.get("plannedDateTime"))),
        "geannuleerd": bool(leg.get("cancelled")),
    }

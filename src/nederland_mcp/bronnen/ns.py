"""NS Reisinformatie API: stations, departures, arrivals, journey planning, disruptions and planned works.

Needs NS_API_KEY (a subscription key from apiportal.ns.nl).
"""

import asyncio
import math
import os
from datetime import date, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from ..common import AMSTERDAM, cached, fmt_datetime, fmt_time, get_json, normalize, now_local, parse_moment, parse_time

BASE = "https://gateway.apiportal.ns.nl/reisinformatie-api/api"
SOURCE = "NS Reisinformatie"
BRON = "NS Reisinformatie API"
CROWD = {"LOW": "rustig", "MEDIUM": "gemiddeld", "HIGH": "druk"}
BOARDS = {"departures": "vertrektijden", "arrivals": "aankomsttijden"}
STATUS = {
    "NORMAL": "normaal",
    "ALTERNATIVE_TRANSPORT": "vervangend vervoer",
    "CANCELLED": "rijdt niet",
    "DISRUPTION": "storing",
    "MAINTENANCE": "werkzaamheden",
    "CHANGE": "gewijzigd",
    "UNCERTAIN": "onzeker",
}
# Without a date, works that start within this time come along as "binnenkort".
SOON = timedelta(days=3)
MAX_DAYS = 31
MAX_ITEMS = 20
MAX_TRIPS = 5
# In comparing trips a transfer counts as this many minutes: it takes effort, and a late train makes you miss the next.
TRANSFER_MINUTES = 5
# The other end of a trip counts for a quarter: each minute you leave later, or for an arrival search each minute you
# arrive earlier. So a much shorter trip beats one that arrives a minute sooner.
OTHER_END = 0.25


def _headers() -> dict:
    key = os.environ.get("NS_API_KEY")
    if not key:
        raise ToolError("Treininformatie is nog niet beschikbaar: de server heeft geen NS API-key (NS_API_KEY).")
    return {"Ocp-Apim-Subscription-Key": key}


async def _messages(kind: str, active: bool, ttl: int) -> list[dict]:
    """NS's messages of one type: DISRUPTION, MAINTENANCE or CALAMITY. Not active means all, also those to come."""
    params = {"type": kind, "isActive": str(active).lower()}
    data = await cached(
        ("ns-disruptions", kind, active),
        ttl,
        lambda: get_json(f"{BASE}/v3/disruptions", params=params, headers=_headers(), source=SOURCE),
    )
    return data if isinstance(data, list) else []


async def berichten() -> list[str]:
    """NS's national travel messages, such as a strike. Empty without a key, or when NS does not answer.

    A failure is kept for as long as a message, so a slow NS does not hold up every question.
    """
    if not os.environ.get("NS_API_KEY"):
        return []

    async def fetch() -> Any:
        try:
            return await _messages("CALAMITY", True, 600)
        except ToolError:
            return []

    data = await cached(("ns-berichten",), 600, fetch)
    texts = []
    for item in data if isinstance(data, list) else []:
        text = (item.get("description") or item.get("title") or "").strip()
        if text:
            texts.append(f"{text} Meer: {item['url']}" if item.get("url") else text)
    return texts


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
    moment = parse_moment(datum, tijd) if datum or tijd else None
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


async def trein_reis(van: str, naar: str, datum: str | None = None, tijd: str | None = None, aankomst: bool = False) -> dict:
    origin, destination = await _station(van), await _station(naar)
    moment = parse_moment(datum, tijd)

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
    # NS starts an arrival search at the first trip arriving after the asked time; earlier ones are on the previous
    # page. Fetch that too when fewer than three trips are on time, so there is something to choose from.
    if aankomst and backward and sum(map(fits, options)) < 3:
        earlier = await search({"context": backward})
        options = _options(earlier.get("trips") or []) + options
    # NS also returns options just outside the asked time, such as a train that already left.
    fitting = [o for o in options if fits(o)]
    running = [o for o in fitting if not _cancelled(o)]
    if running:
        chosen = _pick(running, aankomst)
    else:
        # Nothing on time runs: show what NS has, so the answer can say so.
        chosen = (fitting[-MAX_TRIPS:][::-1] if aankomst else fitting[:MAX_TRIPS]) or options[:MAX_TRIPS]

    trips = []
    for departure, arrival, trip, legs in chosen:
        fare = trip.get("productFare") or {}
        option = {
            "vertrek": fmt_time(departure),
            "aankomst": fmt_time(arrival),
            "reistijd_min": trip.get("actualDurationInMinutes") or trip.get("plannedDurationInMinutes"),
            "overstappen": trip.get("transfers"),
            "status": STATUS.get(trip.get("status"), trip.get("status")),
            "drukte": CROWD.get(trip.get("crowdForecast"), trip.get("crowdForecast")),
            "prijs_euro": fare["priceInCents"] / 100 if fare.get("priceInCents") else None,
            # Intercity direct and ICE cost extra on part of the way; the price above includes it.
            **({"toeslag_euro": fare["supplementInCents"] / 100} if fare.get("supplementInCents") else {}),
            "delen": [_leg(leg) for leg in legs],
            "link": (trip.get("shareUrl") or {}).get("uri"),
        }
        if notes := _notes(trip):
            option["meldingen"] = notes
        trips.append(option)
    if not trips:
        raise ToolError(f"Geen reisadvies gevonden van {_station_name(origin)} naar {_station_name(destination)}.")
    if running:
        trips[0] = {"beste": True, **trips[0]}
    result = {
        "van": _station_name(origin),
        "naar": _station_name(destination),
        "gezocht_op": f"{'aankomst' if aankomst else 'vertrek'} {moment.strftime('%Y-%m-%d %H:%M')}",
        "reisadviezen": trips,
    }
    if running and (gone := [o for o in fitting if _cancelled(o)]):
        # Often the first train runs and a connection does not, so say which trip, not which train.
        result["niet_mogelijk"] = [_describe(o) for o in gone]
    return {**result, "bron": BRON}


def _notes(trip: dict) -> list[str]:
    """NS's messages on a trip, such as works with buses instead of trains, and its labels, such as a shorter train.
    The label with the supplement is left out, since toeslag_euro has it."""
    primary = ((trip.get("primaryMessage") or {}).get("message") or {}).get("text")
    found = [primary, *(m.get("text") for m in trip.get("messages") or [])]
    found += [m.get("text") for leg in trip.get("legs") or [] for m in leg.get("messages") or []]
    labels = [item.get("label") for item in trip.get("labelListItems") or []]
    found += [label for label in labels if label and "toeslag" not in label]
    notes: list[str] = []
    for text in found:
        if text and text.strip() not in notes:
            notes.append(text.strip())
    return notes


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


def _cancelled(option: tuple) -> bool:
    _, _, trip, legs = option
    return trip.get("status") == "CANCELLED" or any(leg.get("cancelled") for leg in legs)


def _describe(option: tuple) -> str:
    departure, arrival, _, legs = option
    via = [(leg.get("destination") or {}).get("name") or "?" for leg in legs[:-1]]
    return f"vertrek {fmt_time(departure)}, aankomst {fmt_time(arrival)}" + (f", via {', '.join(via)}" if via else "")


def _pick(options: list[tuple], aankomst: bool) -> list[tuple]:
    """The best trip first, then the others worth taking in time order, MAX_TRIPS in all.

    The best arrives first, or for an arrival search leaves last, where each transfer counts as TRANSFER_MINUTES and
    the other end of the trip as OTHER_END. A trip is not worth taking when another one leaves no earlier (for an
    arrival search: arrives no later), costs no more and has no more transfers, unless it arrives earlier (leaves
    later) by more than its extra transfers are worth. So a trip with two changes that arrives a minute before the
    direct train drops out.
    """
    def minutes(moment: datetime) -> float:
        return moment.timestamp() / 60

    def transfers(option: tuple) -> int:
        return option[2].get("transfers") or 0

    def price(option: tuple) -> float:
        return (option[2].get("productFare") or {}).get("priceInCents") or math.inf

    def order(option: tuple) -> tuple:
        departure, arrival = minutes(option[0]), minutes(option[1])
        extra = TRANSFER_MINUTES * transfers(option)
        if aankomst:
            return extra - departure + OTHER_END * arrival, transfers(option), arrival, price(option)
        return arrival + extra - OTHER_END * departure, transfers(option), -departure, price(option)

    def beats(a: tuple, b: tuple) -> bool:
        if transfers(a) > transfers(b) or price(a) > price(b):
            return False
        margin = TRANSFER_MINUTES * (transfers(b) - transfers(a))
        if aankomst:
            return a[1] <= b[1] and minutes(a[0]) >= minutes(b[0]) - margin
        return a[0] >= b[0] and minutes(a[1]) <= minutes(b[1]) + margin

    kept: list[tuple] = []
    for option in sorted(options, key=order):
        if not any(beats(other, option) for other in kept):
            kept.append(option)
    rest = sorted(kept[1:], key=lambda o: o[0])
    # For an arrival search the trips just before the asked time matter most, otherwise the ones just after it.
    return [kept[0], *(rest[1 - MAX_TRIPS:] if aankomst else rest[: MAX_TRIPS - 1])]


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


def _period(datum: str | None, tot: str | None) -> tuple[date, date] | None:
    """The asked days, from datum up to and including tot. None means now and the coming days."""
    if not datum and not tot:
        return None
    today = now_local().date()
    try:
        first = date.fromisoformat(datum.strip()) if datum else today
        last = date.fromisoformat(tot.strip()) if tot else first
    except ValueError as exc:
        raise ToolError(f"Gebruik de datum als JJJJ-MM-DD, bijvoorbeeld {today.isoformat()}.") from exc
    first, last = min(first, last), max(first, last)
    if last < today:
        raise ToolError("NS geeft alleen storingen van nu en werkzaamheden die nog komen, niet die van eerdere dagen.")
    first = max(first, today)
    if (last - first).days >= MAX_DAYS:
        raise ToolError(f"Vraag hooguit {MAX_DAYS} dagen tegelijk.")
    return first, last


def _codes(item: dict) -> set[str]:
    """Station codes on the stretch of track a message is about."""
    codes: set[str] = set()
    for part in item.get("publicationSections") or []:
        for section in (part.get("section") or {}, (part.get("consequence") or {}).get("section") or {}):
            codes.update(s["stationCode"] for s in section.get("stations") or [] if s.get("stationCode"))
    return codes


def _spans(item: dict) -> list[tuple[datetime, datetime | None, dict]]:
    """The periods of a message, each with its own text. Without an end it lasts until further notice."""
    spans = []
    for span in item.get("timespans") or [{}]:
        start = parse_time(span.get("start") or item.get("start"))
        if start:
            spans.append((start, parse_time(span.get("end") or item.get("end")), span))
    return spans


def _row(item: dict, span: dict, start: datetime, end: datetime | None, code: str | None) -> dict:
    # Most works only say "De extra reistijd verschilt per reis.", which tells nothing.
    travel = (span.get("additionalTravelTime") or {}).get("label") or ""
    row: dict[str, Any] = {
        "traject": (item.get("title") or "").strip().rstrip("."),
        "wat": (span.get("situation") or {}).get("label"),
        "wanneer": span.get("period") or item.get("period")
        or f"vanaf {fmt_datetime(start)}" + (f" tot {fmt_datetime(end)}" if end else ""),
        "verwachting": (item.get("expectedDuration") or {}).get("description"),
        "extra_reistijd": travel if "verschilt per reis" not in travel else None,
    }
    # The buses or taxis that run in this period, and where they leave at the asked station.
    transport, leaves = [], None
    for alternative in item.get("alternativeTransportTimespans") or []:
        a_start, a_end = parse_time(alternative.get("start")), parse_time(alternative.get("end"))
        if (a_start and end and a_start >= end) or (a_end and a_end <= start):
            continue
        offered = alternative.get("alternativeTransport") or {}
        if offered.get("label") and offered["label"] not in transport:
            transport.append(offered["label"])
        for location in offered.get("location") or []:
            if code and (location.get("station") or {}).get("stationCode") == code and location.get("description"):
                leaves = leaves or location["description"]
    row["vervangend_vervoer"] = transport
    row["vervangend_vervoer_vertrekt"] = leaves
    row["advies"] = " ".join(advice.strip() for advice in span.get("advices") or [] if advice and advice.strip())
    return {key: value for key, value in row.items() if value}


async def trein_storingen(station: str | None = None, datum: str | None = None, tot: str | None = None) -> dict:
    days = _period(datum, tot)
    found = await _station(station) if station and station.strip() else None
    code = found.get("code") if found else None
    disruptions, works, calamities = await asyncio.gather(
        _messages("DISRUPTION", True, 120),
        _messages("MAINTENANCE", False, 900),
        _messages("CALAMITY", True, 600),
    )
    now = now_local()
    if days:
        begin = datetime.combine(days[0], datetime.min.time(), AMSTERDAM)
        until = datetime.combine(days[1] + timedelta(days=1), datetime.min.time(), AMSTERDAM)
    else:
        begin, until = now, now + SOON

    def rows(items: list[dict], keep: Any) -> list[dict]:
        kept: list[tuple[datetime, dict]] = []
        for item in items:
            if code and code not in _codes(item):
                continue
            for start, end, span in _spans(item):
                if not keep(start, end):
                    continue
                row = _row(item, span, start, end, code)
                if all(row != other for _, other in kept):
                    kept.append((start, row))
        return [row for _, row in sorted(kept, key=lambda pair: pair[0])]

    def during(start: datetime, end: datetime | None) -> bool:
        return start < until and (end is None or end > begin)

    where = f" op het spoor langs {_station_name(found)}" if found else ""
    result: dict[str, Any] = {}
    if found:
        result["station"] = _station_name(found)
    if days:
        result["periode"] = f"{days[0].isoformat()} tot en met {days[1].isoformat()}"
    national = [
        {key: value for key, value in (("titel", item.get("title")), ("bericht", item.get("description")),
                                       ("meer", item.get("url"))) if value}
        for item in calamities
    ]
    if national:
        result["landelijke_berichten"] = national

    def add(key: str, found_rows: list[dict], empty: str) -> None:
        result[key] = found_rows[:MAX_ITEMS] or empty
        if len(found_rows) > MAX_ITEMS:
            result[f"{key}_niet_getoond"] = (
                f"Nog {len(found_rows) - MAX_ITEMS} meer. Noem een station voor alleen het spoor daarlangs.")

    add("storingen", rows(disruptions, during), f"Geen storingen bekend{where}.")
    if days:
        add("werkzaamheden", rows(works, during), f"Geen werkzaamheden{where} in deze periode.")
    else:
        add("werkzaamheden_nu", rows(works, lambda start, end: start <= now and (end is None or end > now)),
            f"Nu geen werkzaamheden{where}.")
        add("werkzaamheden_binnenkort", rows(works, lambda start, end: now < start < until),
            f"Geen werkzaamheden{where} in de komende drie dagen.")
    result["bron"] = BRON
    return result

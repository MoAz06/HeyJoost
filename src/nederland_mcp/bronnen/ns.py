"""NS Reisinformatie API: stations, departures, arrivals, journey planning, disruptions and planned works.

Needs NS_API_KEY (a subscription key from apiportal.ns.nl).
"""

import asyncio
import math
import os
import re
from datetime import date, datetime, time, timedelta
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
# A tight one counts twice.
TRANSFER_MINUTES = 5
# The other end of a trip counts for a quarter: each minute you leave later, or for an arrival search each minute you
# arrive earlier. So a much shorter trip beats one that arrives a minute sooner.
OTHER_END = 0.25
# A transfer with fewer minutes than this is tight; from AMPLE on there is plenty of time, in between it is enough.
TIGHT = 5
AMPLE = 10
# To be somewhere at a time, the train arrives this much before the last stretch, in case it runs late.
MARGIN = 5
# A train that leaves in fewer minutes than this you only catch when you are at it already, so it is not the best trip.
LEAVING = 2
# Until this hour a train still belongs to the evening before, and from NIGHT on it is a night train.
NIGHT_ENDS = 4
NIGHT = 1
# For the last train, NS is asked page by page back from the end of the night until this many trips of the evening.
LAST_EVENING = 3
# NS's kinds of station, from the biggest down: a town with more stations finds its biggest, and a place without a
# station hears the biggest one near it.
SIZES = (
    "MEGA_STATION", "KNOOPPUNT_INTERCITY_STATION", "INTERCITY_STATION", "KNOOPPUNT_SNELTREIN_STATION",
    "SNELTREIN_STATION", "KNOOPPUNT_STOPTREIN_STATION", "STOPTREIN_STATION", "FACULTATIEF_STATION",
)
# Words Haiku puts around a town's station that NS does not use: Gouda Centraal and Station Hilversum are Gouda and
# Hilversum, and in English Utrecht Central is Utrecht Centraal.
EXTRA_WORDS = {"station", "centraal", "central", "cs", "ns"}
# A place this close to a station, some 13 minutes on foot, is a walk from it. Further away, the biggest station may
# be up to FURTHER times as far as the nearest, but no more than NEAR: Utrecht Centraal for the Science Park.
WALK = 800
FURTHER = 2
NEAR = 15_000
WEEKDAYS = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")
MONTHS = (
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
)


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


def _names(station: dict) -> list[str]:
    namen = station.get("namen") or {}
    return [n for n in (station.get("code", ""), *namen.values(), *(station.get("synoniemen") or [])) if n]


def _words(text: str) -> list[str]:
    """The words of a name, written as normalize writes them."""
    return re.findall(r"[a-z0-9]+", text.lower().replace("'s-", "s").replace("ij", "y"))


def _size(station: dict) -> int:
    kind = station.get("stationType")
    return SIZES.index(kind) if kind in SIZES else len(SIZES)


def _match(name: str, stations: list[dict]) -> dict | None:
    """The station with this name, or None. Of several, the one in the Netherlands and then the biggest."""
    q = normalize(name)

    def rank(station: dict) -> int:
        normalized = [normalize(n) for n in _names(station)]
        if q in normalized:
            return 0
        if any(n.startswith(q) for n in normalized):
            return 1
        if any(q in n for n in normalized):
            return 2
        return 3

    def order(station: dict) -> tuple:
        return station.get("land") != "NL", _size(station)

    ranked = sorted((s for s in stations if rank(s) < 3), key=lambda s: (rank(s), *order(s)))
    if ranked:
        return ranked[0]
    # Such as Gouda Centraal or Station Hilversum: the town's own station, or with more stations its biggest, as
    # Almere Centrum for Almere Centraal. Only whole words, so Station Zuid is no Zuidhorn, and only in the
    # Netherlands: Brussels Central is not Brussels Airport.
    asked = _words(name)
    town = [word for word in asked if word not in EXTRA_WORDS]
    if not town or len(town) == len(asked):
        return None

    def town_rank(station: dict) -> int:
        words = [_words(n) for n in _names(station)]
        if town in words:
            return 0
        if any(len(w) > len(town) and w[: len(town)] == town for w in words):
            return 1
        return 2

    dutch = (s for s in stations if s.get("land") == "NL" and town_rank(s) < 2)
    ranked = sorted(dutch, key=lambda s: (town_rank(s), _size(s)))
    return ranked[0] if ranked else None


async def _station(name: str) -> dict:
    stations = await _stations()
    found = _match(name, stations)
    if found is None:
        raise ToolError(await _no_station(name, stations))
    return found


async def _no_station(name: str, stations: list[dict]) -> str:
    """Why there is no train for this name, with the stations near the place it names. Without them Haiku guessed:
    for Utrecht Science Park it tried 'Utrecht Science Park Galgenwaard' and 'Bèta' before it took Utrecht Centraal."""
    # ov finds stops and places at Transitous, and uses this module itself for the stations.
    from . import ov

    try:
        point, stop, _, sure = await ov._origin(name, None)
    except ToolError:
        point, stop, sure = None, None, True
    # A stop found on the town alone, as Rotterdam Beurs for Rotterdam Ahoy, says little: the place itself first,
    # and the stop only when it shares a word of its own with the name.
    if stop and not sure:
        try:
            point = await ov._spot(name, None)
        except ToolError:
            point = None
        town = ov._town_words(stop)
        if not (set(ov._words(ov._short(stop["name"]))) - town) & (set(ov._words(name)) - town):
            stop = None
    place = point or stop
    nearby = _nearby(place, name, stations) if place else None
    if nearby is None:
        return (
            f"Geen NS-station gevonden voor '{name}'. Is het een halte van bus, tram of metro, zoek dan de trein naar "
            "het station in de buurt, en het laatste stuk met ov_vertrek."
        )
    return f"'{name}' is geen NS-station. {nearby}"


def _nearby(place: dict, name: str, stations: list[dict]) -> str | None:
    """The stations near a place and how to go on from there, or None without stations."""
    from . import ov

    # Without departures, as Utrecht Maliebaan for the railway museum, a station is no way to get there.
    nl = [s for s in stations if s.get("land") == "NL" and s.get("lat") and s.get("heeftVertrektijden")]
    if not nl:
        return None

    def far(station: dict) -> float:
        return ov._distance(place, {"lat": station["lat"], "lon": station["lng"]})

    nearest = min(nl, key=far)
    if far(nearest) <= WALK:
        walk = ov._walk(far(nearest))
        return (
            f"{_station_name(nearest)} ligt op {max(10, round(far(nearest), -1)):.0f} meter, ongeveer {walk} "
            f"{'minuut' if walk == 1 else 'minuten'} lopen: zoek de trein van of naar dat station."
        )
    reach = min(NEAR, FURTHER * far(nearest))
    biggest = min((s for s in nl if far(s) <= reach), key=lambda s: (_size(s), far(s)))
    text = f"Het dichtstbijzijnde station is {_station_name(nearest)}, op {_km(far(nearest))}"
    if biggest is not nearest and _size(biggest) < _size(nearest):
        text += (
            f", en het grootste in de buurt {_station_name(biggest)}, op {_km(far(biggest))}, met vaak de meeste "
            "bussen en trams"
        )
        which = "een van die stations"
    else:
        which = "dat station"
    return f"{text}. Zoek de trein van of naar {which}, en het stuk tussen het station en '{name}' met ov_vertrek."


async def _seldom(ends: list[tuple[dict, str]]) -> str:
    """For a station where a train only stops now and then, as Utrecht Maliebaan at the railway museum: the stations
    near it where trains do stop. Each end comes with the name that was asked for it."""
    for station, asked in ends:
        if not station.get("heeftVertrektijden") and station.get("lat"):
            nearby = _nearby({"lat": station["lat"], "lon": station["lng"]}, asked, await _stations())
            if nearby:
                return f" Bij {_station_name(station)} stopt maar af en toe een trein. {nearby}"
    return ""


def _km(metres: float) -> str:
    return f"{metres / 1000:.1f} km".replace(".", ",")


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


async def trein_reis(
    van: str,
    naar: str,
    datum: str | None = None,
    tijd: str | None = None,
    aankomst: bool = False,
    lopen_min: int | None = None,
    laatste: bool = False,
) -> dict:
    origin, destination = await _station(van), await _station(naar)
    if laatste:
        return await _last(origin, destination, datum)
    asked = parse_moment(datum, tijd)
    # The last stretch and the margin come off here: Haiku often forgot to do that itself.
    before = lopen_min + MARGIN if aankomst and lopen_min is not None else 0
    moment = asked - timedelta(minutes=before)

    params = {
        "fromStation": origin.get("code"),
        "toStation": destination.get("code"),
        "dateTime": moment.isoformat(timespec="seconds"),
        "searchForArrival": str(aankomst).lower(),
    }

    def fits(option: tuple) -> bool:
        return option[1] <= moment if aankomst else option[0] >= moment

    data = await _trips(params)
    options = _options(data.get("trips") or [])
    backward = data.get("scrollRequestBackwardContext")
    # NS starts an arrival search at the first trip arriving after the asked time; earlier ones are on the previous
    # page. Fetch that too when fewer than three trips are on time, so there is something to choose from.
    if aankomst and backward and sum(map(fits, options)) < 3:
        earlier = await _trips({**params, "context": backward})
        options = _unique(_options(earlier.get("trips") or []) + options)
    # A departure search starts a few minutes early: after a row of trains that just left, at 20:59 in Breda all five
    # trips on the page had gone. The next page has the ones still to come.
    forward = data.get("scrollRequestForwardContext")
    if not aankomst and forward and sum(map(fits, options)) < 3:
        later = await _trips({**params, "context": forward})
        options = _unique(options + _options(later.get("trips") or []))
    # NS also returns options just outside the asked time, such as a train that already left.
    fitting = [o for o in options if fits(o)]
    now = now_local()
    # Searching from about now, a train that left more than a minute ago is no use, as long as there is another.
    near = moment >= now - timedelta(minutes=5)
    if near:
        fitting = [o for o in fitting if o[0] >= now - timedelta(minutes=1)] or fitting
    running = [o for o in fitting if not _cancelled(o)]
    if running:
        chosen = _pick(running, aankomst, now if near else None)
    else:
        # Nothing on time runs: show what NS has, so the answer can say so.
        chosen = (fitting[-MAX_TRIPS:][::-1] if aankomst else fitting[:MAX_TRIPS]) or options[:MAX_TRIPS]

    trips = [_advice(option, near and _leaving(option[0], now), lopen_min if aankomst else None) for option in chosen]
    if not trips:
        raise ToolError(
            f"Geen reisadvies gevonden van {_station_name(origin)} naar {_station_name(destination)}."
            + await _seldom([(origin, van), (destination, naar)])
        )
    if running:
        trips[0] = {"beste": True, **trips[0]}
    result: dict[str, Any] = {
        "van": _station_name(origin),
        "naar": _station_name(destination),
        "gezocht_op": f"{'aankomst' if aankomst else 'vertrek'} {moment.strftime('%Y-%m-%d %H:%M')}",
        "dag": _day(moment),
    }
    if before:
        walk = f"{lopen_min} minuten lopen en " if lopen_min else ""
        result["op_tijd_voor"] = f"{fmt_time(asked)}, met {walk}{MARGIN} minuten marge"
    result["reisadviezen"] = trips
    if running and (gone := [o for o in fitting if _cancelled(o)]):
        # Often the first train runs and a connection does not, so say which trip, not which train.
        result["niet_mogelijk"] = [_describe(o) for o in gone]
    return {**result, "bron": BRON}


async def _last(origin: dict, destination: dict, datum: str | None) -> dict:
    """The last trains of an evening, the very last first, and the night trains after them.

    NS searches forward from a time, so this starts at the end of the night and goes back page by page. Without a
    date, just after midnight it is still the night before.
    """
    now = now_local()
    day = parse_moment(datum, None).date()
    if datum is None and now.hour < NIGHT_ENDS:
        day -= timedelta(days=1)
    end = datetime.combine(day + timedelta(days=1), time(NIGHT_ENDS), tzinfo=AMSTERDAM)
    night = end.replace(hour=NIGHT)
    params = {
        "fromStation": origin.get("code"),
        "toStation": destination.get("code"),
        "dateTime": end.isoformat(timespec="seconds"),
        "searchForArrival": "false",
    }
    data = await _trips(params)
    after = _options(data.get("trips") or [])
    options = [option for option in after if option[0] < end]
    context = data.get("scrollRequestBackwardContext")
    for _ in range(3):
        if not context or sum(option[0] < night for option in options) >= LAST_EVENING:
            break
        page = await _trips({**params, "context": context})
        options = [option for option in _options(page.get("trips") or []) if option[0] < end] + options
        context = page.get("scrollRequestBackwardContext")
    options = sorted(_unique(options), key=lambda option: option[0])
    if now < end:
        options = [option for option in options if option[0] >= now - timedelta(minutes=1)]
    running = [option for option in options if not _cancelled(option)]
    evening = [option for option in running if option[0] < night]
    late = [option for option in running if option[0] >= night]
    chosen = _latest(evening or late)
    if not chosen:
        first = next((option for option in after if option[0] >= end and not _cancelled(option)), None)
        then = f" De eerste gaat {_day(first[0])} om {fmt_time(first[0])}." if first else ""
        raise ToolError(
            f"Er gaat geen trein meer van {_station_name(origin)} naar {_station_name(destination)} op de avond van "
            f"{WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]}.{then}"
        )

    trips = [_advice(option, _leaving(option[0], now)) for option in chosen]
    trips[0] = {"laatste": True, **trips[0]}
    result: dict[str, Any] = {
        "van": _station_name(origin),
        "naar": _station_name(destination),
        "gezocht_op": f"laatste trein {day.isoformat()}",
        # Just after midnight "gisteren" would read as if the trains had gone.
        "dag": f"{WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]} (vannacht)"
        if end.replace(hour=0) <= now < end else _day(end - timedelta(days=1)),
        # A sentence, before the trips: as a list Haiku left the night trains out, or said they leave earlier.
        **({"nachttreinen": _night_trains(late)} if evening and late else {}),
        "reisadviezen": trips,
    }
    if gone := [option for option in options if _cancelled(option)]:
        result["niet_mogelijk"] = [_describe(option) for option in gone]
    return {**result, "bron": BRON}


def _latest(options: list[tuple]) -> list[tuple]:
    """The trip that leaves last first, then the ones before it in time order, MAX_TRIPS in all. A trip drops out
    when another leaves no earlier, arrives no later, and has no more transfers and no higher price."""
    def transfers(option: tuple) -> int:
        return option[2].get("transfers") or 0

    def price(option: tuple) -> float:
        return (option[2].get("productFare") or {}).get("priceInCents") or math.inf

    def beats(a: tuple, b: tuple) -> bool:
        return a[1] <= b[1] and transfers(a) <= transfers(b) and price(a) <= price(b)

    kept: list[tuple] = []
    for option in sorted(options, key=lambda o: (-o[0].timestamp(), o[1], transfers(o))):
        if not any(beats(other, option) for other in kept):
            kept.append(option)
    return kept[:1] + sorted(kept[1:MAX_TRIPS], key=lambda option: option[0])


def _night_trains(options: list[tuple]) -> str:
    """The trains after the last one of the evening, in a sentence."""
    parts = []
    for departure, arrival, trip, _ in options:
        transfers = trip.get("transfers") or 0
        change = f", {transfers} {'overstap' if transfers == 1 else 'overstappen'}" if transfers else ""
        parts.append(f"om {fmt_time(departure)} (aankomst {fmt_time(arrival)}{change})")
    listed = f"{', '.join(parts[:-1])} en {parts[-1]}" if len(parts) > 1 else parts[0]
    return f"Daarna gaan er 's nachts nog treinen: {listed}."


async def _trips(params: dict) -> dict:
    """A page of trips from NS, kept for a minute."""
    return await cached(
        ("ns-reis", tuple(params.items())),
        60,
        lambda: get_json(f"{BASE}/v3/trips", params=params, headers=_headers(), source=SOURCE),
    )


def _advice(option: tuple, leaving: bool, last_stretch: int | None = None) -> dict[str, Any]:
    """A trip as trein_reis gives it; with the minutes of the last stretch also when you are there."""
    departure, arrival, trip, legs = option
    fare = trip.get("productFare") or {}
    changes = _changes(legs)
    advice = {
        "vertrek": fmt_time(departure),
        # Named, but not the best: Haiku once said a train that left two minutes late had gone.
        **({"vertrekt_nu": True} if leaving else {}),
        "aankomst": fmt_time(arrival),
        # Haiku added the last stretch itself and rounded: 08:48 and a quarter of an hour became "rond 09:05".
        **({"op_bestemming": fmt_time(arrival + timedelta(minutes=last_stretch))} if last_stretch else {}),
        "reistijd_min": trip.get("actualDurationInMinutes") or trip.get("plannedDurationInMinutes"),
        "overstappen": trip.get("transfers"),
        # In words: from only the numbers Haiku wrote "die stopt niet over" or "stopt niet op Schiphol" for a train
        # that stops eight times. Replaying about 420 answers each way, such slips went from 19 to 3.
        **({"zonder_overstap": _direct(legs[0])} if len(legs) == 1 and _is_train(legs[0]) else {}),
        # Worked out here, since Haiku called five minutes tight.
        **({"overstaptijd": [_change_line(station, gap) for station, gap in changes]} if changes else {}),
        "status": STATUS.get(trip.get("status"), trip.get("status")),
        "drukte": CROWD.get(trip.get("crowdForecast"), trip.get("crowdForecast")),
        "prijs_euro": fare["priceInCents"] / 100 if fare.get("priceInCents") else None,
        # Intercity direct and ICE cost extra on part of the way; the price above includes it.
        **({"toeslag_euro": fare["supplementInCents"] / 100} if fare.get("supplementInCents") else {}),
        "delen": [_leg(leg) for leg in legs],
        "link": (trip.get("shareUrl") or {}).get("uri"),
    }
    if notes := _notes(trip):
        advice["meldingen"] = notes
    return advice


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


def _is_train(leg: dict) -> bool:
    return leg.get("travelType", "PUBLIC_TRANSIT") == "PUBLIC_TRANSIT"


def _between(leg: dict) -> int | None:
    """How often a train stops between where you get on and off, or None when NS does not say."""
    stops = [stop for stop in leg.get("stops") or [] if not stop.get("passing") and not stop.get("cancelled")]
    return len(stops) - 2 if len(stops) >= 2 else None


def _direct(leg: dict) -> str:
    """A trip in one train, in words."""
    between = _between(leg)
    if between is None:
        return "Je blijft de hele reis in dezelfde trein."
    often = "nergens" if between == 0 else "1 keer" if between == 1 else f"{between} keer"
    return f"Je blijft de hele reis in dezelfde trein, en die stopt onderweg {often}."


def _leaving(departure: datetime, now: datetime) -> bool:
    """Whether a train leaves right now: within LEAVING minutes, or less than a minute ago."""
    return now - timedelta(minutes=1) <= departure < now + timedelta(minutes=LEAVING)


def _expected(place: dict | None) -> datetime | None:
    """When a train leaves or arrives somewhere, as expected now."""
    place = place or {}
    return parse_time(place.get("actualDateTime") or place.get("plannedDateTime"))


def _day(moment: datetime) -> str:
    """The day in words, such as 'maandag 26 oktober (over 3 dagen)': Haiku called a Monday three days ahead today."""
    ahead = (moment.date() - now_local().date()).days
    when = {-1: "gisteren", 0: "vandaag", 1: "morgen", 2: "overmorgen"}.get(ahead)
    when = when or (f"over {ahead} dagen" if ahead > 0 else f"{-ahead} dagen geleden")
    return f"{WEEKDAYS[moment.weekday()]} {moment.day} {MONTHS[moment.month - 1]} ({when})"


def _changes(legs: list[dict]) -> list[tuple[str, int]]:
    """Where you change trains and how many minutes you have there, from the arrival to the next departure as expected
    now. A walk in between comes off."""
    changes: list[tuple[str, int]] = []
    walk, last = 0, None
    for leg in legs:
        if not _is_train(leg):
            walk += leg.get("plannedDurationInMinutes") or 0
            continue
        departure = _expected(leg.get("origin"))
        if last and last[1] and departure:
            changes.append((last[0], round((departure - last[1]).total_seconds() / 60) - walk))
        destination = leg.get("destination") or {}
        last, walk = (destination.get("name") or "?", _expected(destination)), 0
    return changes


def _change_line(station: str, gap: int) -> str:
    if gap < 0:
        return f"{station}: haal je niet, de trein vertrekt voor je aankomt"
    verdict = "krap" if gap < TIGHT else "genoeg" if gap < AMPLE else "ruim"
    return f"{station}: {gap} {'minuut' if gap == 1 else 'minuten'}, {verdict}"


def _options(trips: list[dict]) -> list[tuple[datetime, datetime, dict, list[dict]]]:
    options = []
    for trip in trips:
        legs = trip.get("legs") or []
        if not legs:
            continue
        departure, arrival = _expected(legs[0].get("origin")), _expected(legs[-1].get("destination"))
        if departure and arrival:
            options.append((departure, arrival, trip, legs))
    return options


def _unique(options: list[tuple]) -> list[tuple]:
    """The options without a trip that is on two pages of NS, in the same order."""
    return list({option[2].get("uid") or _describe(option): option for option in options}.values())


def _cancelled(option: tuple) -> bool:
    _, _, trip, legs = option
    return trip.get("status") == "CANCELLED" or any(leg.get("cancelled") for leg in legs)


def _describe(option: tuple) -> str:
    departure, arrival, _, legs = option
    via = [(leg.get("destination") or {}).get("name") or "?" for leg in legs[:-1]]
    return f"vertrek {fmt_time(departure)}, aankomst {fmt_time(arrival)}" + (f", via {', '.join(via)}" if via else "")


def _pick(options: list[tuple], aankomst: bool, now: datetime | None = None) -> list[tuple]:
    """The best trip first, then the others worth taking in time order, MAX_TRIPS in all.

    The best arrives first, or for an arrival search leaves last, where each transfer counts as TRANSFER_MINUTES, a
    tight one twice, and the other end of the trip as OTHER_END. A trip is not worth taking when another one leaves no
    earlier (for an arrival search: arrives no later), costs no more and has no more transfers, nor more tight ones,
    unless it arrives earlier (leaves later) by more than its extra transfers are worth. So a trip with two changes
    that arrives a minute before the direct train drops out.

    With `now`, a train that leaves within LEAVING minutes is only the best when no other is, and it does not push out
    a trip you can still catch.
    """
    def minutes(moment: datetime) -> float:
        return moment.timestamp() / 60

    def transfers(option: tuple) -> int:
        return option[2].get("transfers") or 0

    def tight(option: tuple) -> int:
        return sum(gap < TIGHT for _, gap in _changes(option[3]))

    def weight(option: tuple) -> int:
        return TRANSFER_MINUTES * (transfers(option) + tight(option))

    def price(option: tuple) -> float:
        return (option[2].get("productFare") or {}).get("priceInCents") or math.inf

    def order(option: tuple) -> tuple:
        departure, arrival = minutes(option[0]), minutes(option[1])
        if aankomst:
            return weight(option) - departure + OTHER_END * arrival, transfers(option), arrival, price(option)
        return arrival + weight(option) - OTHER_END * departure, transfers(option), -departure, price(option)

    def beats(a: tuple, b: tuple) -> bool:
        if transfers(a) > transfers(b) or tight(a) > tight(b) or price(a) > price(b):
            return False
        margin = weight(b) - weight(a)
        if aankomst:
            return a[1] <= b[1] and minutes(a[0]) >= minutes(b[0]) - margin
        return a[0] >= b[0] and minutes(a[1]) <= minutes(b[1]) + margin

    def catchable(option: tuple) -> bool:
        return now is None or option[0] >= now + timedelta(minutes=LEAVING)

    kept: list[tuple] = []
    for option in sorted(options, key=order):
        if not any(beats(other, option) for other in kept if catchable(other) or not catchable(option)):
            kept.append(option)
    best = next((option for option in kept if catchable(option)), kept[0])
    rest = sorted((option for option in kept if option is not best), key=lambda o: o[0])
    # For an arrival search the trips just before the asked time matter most, otherwise the ones just after it.
    return [best, *(rest[1 - MAX_TRIPS:] if aankomst else rest[: MAX_TRIPS - 1])]


def _leg(leg: dict) -> dict[str, Any]:
    origin, destination = leg.get("origin") or {}, leg.get("destination") or {}
    if leg.get("travelType") == "WALK":
        # NS walks the last bit to a station where trains seldom stop, as from Rotterdam Zuid to Rotterdam Stadion.
        return {
            "lopen_min": leg.get("plannedDurationInMinutes"),
            "van": origin.get("name"),
            "vertrek": fmt_time(_expected(origin)),
            "naar": destination.get("name"),
            "aankomst": fmt_time(_expected(destination)),
        }
    product = leg.get("product") or {}
    planned = parse_time(origin.get("plannedDateTime"))
    actual = parse_time(origin.get("actualDateTime"))
    # Without the count Haiku said a train without transfers makes no stops.
    between = _between(leg)
    return {
        "trein": product.get("displayName") or product.get("shortCategoryName") or leg.get("name"),
        "richting": leg.get("direction"),
        "van": origin.get("name"),
        "vertrek": fmt_time(planned),
        "vertraging_min": _delay_minutes(planned, actual),
        "spoor": origin.get("actualTrack") or origin.get("plannedTrack"),
        "naar": destination.get("name"),
        "aankomst": fmt_time(_expected(destination)),
        **({"tussenstops": between} if between is not None else {}),
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

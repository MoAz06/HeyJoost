"""Electricity prices per quarter of an hour: the day-ahead market price in the Netherlands, without and with
energy tax and btw.

The Bundesnetzagentur publishes these prices on SMARD.de under CC BY 4.0, Dutch prices included, so every answer
names it. They come through the Energy-Charts API of Fraunhofer ISE, which allows two requests a minute from one
server, and straight from SMARD when Energy-Charts says no. Today and tomorrow come in one request, shared by
everyone. Energy tax is set by law once a year, so it is written down in TAX: CBS publishes it a month late and
locks its tables for hours at every release.
"""

import time
from collections import deque
from datetime import date, datetime, timedelta
from statistics import mean
from typing import NamedTuple

from mcp.server.mcpserver.exceptions import ToolError

from ..common import AMSTERDAM, cached, get_json, now_local

ENERGY_CHARTS = "https://api.energy-charts.info/price"
# Chart 256 is the market price of the Netherlands, in files of a week.
SMARD = "https://www.smard.de/app/chart_data/256/DE"
BRON = (
    "Bundesnetzagentur | SMARD.de (CC BY 4.0), via Energy-Charts; omgerekend naar cent per kWh, met "
    "energiebelasting en btw"
)
# Energy tax on electricity in euro per kWh without btw, for households (the first 10,000 kWh a year): 2026 from
# the Belastingdienst, 2027 from the corrected Belastingplan 2027. Check 2027 once parliament has voted on it.
TAX = {2026: 0.09161, 2027: 0.08800}
BTW = 0.21
DAYS_BACK = 31
# Energy-Charts allows two requests a minute; past that, SMARD answers.
PER_MINUTE = 2
QUARTER = 15 * 60
# The cheapest stretch of time, in quarters: for a quick job, an hour, and a washing machine or dishwasher.
STRETCHES = (("kwartier", 1), ("uur", 4), ("drie_uur", 12))
WEEKDAYS = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")
TOMORROW = "De prijzen voor morgen komen in de loop van de middag, na de veiling van 12:00. Vraag het later nog eens."
EXPLAIN = (
    "Kaal is de prijs op de stroombeurs (day-ahead), zonder belasting en btw. Met belasting is kaal plus "
    "energiebelasting, met 21% btw over het geheel: wat je met een dynamisch contract per kWh betaalt, zonder de "
    "opslag van je leverancier. Vaste leveringskosten en netbeheerkosten komen er nog bij, en de vermindering "
    "energiebelasting gaat er per jaar af."
)


class Quarter(NamedTuple):
    at: int  # Unix seconds at the start
    kaal: float  # cents per kWh
    met_belasting: float

    @property
    def start(self) -> datetime:
        return datetime.fromtimestamp(self.at, AMSTERDAM)

    @property
    def end(self) -> datetime:
        return datetime.fromtimestamp(self.at + QUARTER, AMSTERDAM)


_sent: deque[float] = deque()


def _may_ask() -> bool:
    """Whether one more request to Energy-Charts fits in the last minute; if so, it is counted."""
    now = time.monotonic()
    while _sent and now - _sent[0] >= 60:
        _sent.popleft()
    if len(_sent) >= PER_MINUTE:
        return False
    _sent.append(now)
    return True


async def _energy_charts(first: date, last: date) -> list[tuple[int, float]]:
    params = {"bzn": "NL", "start": first.isoformat(), "end": last.isoformat()}
    data = await get_json(ENERGY_CHARTS, params=params, source="Energy-Charts")
    if not isinstance(data, dict):
        return []
    moments, prices = data.get("unix_seconds") or [], data.get("price") or []
    return [(int(moment), price) for moment, price in zip(moments, prices) if price is not None]


async def _smard(first: date, last: date) -> list[tuple[int, float]]:
    index = await cached(
        ("smard-index",), 15 * 60, lambda: get_json(f"{SMARD}/index_quarterhour.json", source="SMARD")
    )
    weeks = sorted(index.get("timestamps") or [])
    needed, day = set(), first
    while day <= last:
        midnight = datetime.combine(day, datetime.min.time(), tzinfo=AMSTERDAM).timestamp() * 1000
        started = [week for week in weeks if week <= midnight]
        if started:
            needed.add(started[-1])
        day += timedelta(days=1)
    rows = []
    for week in sorted(needed):
        data = await cached(
            ("smard", week), 15 * 60,
            lambda week=week: get_json(f"{SMARD}/256_DE_quarterhour_{week}.json", source="SMARD"),
        )
        rows += [(moment // 1000, price) for moment, price in data.get("series") or [] if price is not None]
    return rows


async def _prices(first: date, last: date) -> list[tuple[int, float]]:
    """Market prices per quarter of an hour in euro per MWh, for whole local days from first to last."""

    async def fetch() -> list[tuple[int, float]]:
        if _may_ask():
            try:
                rows = await _energy_charts(first, last)
            except ToolError:
                rows = []
            if rows:
                return rows
        try:
            return await _smard(first, last)
        except ToolError as exc:
            raise ToolError("De stroomprijzen zijn nu niet op te halen. Probeer het zo nog eens.") from exc

    # Past days stay the same; for today and tomorrow, a quarter of an hour picks up tomorrow's prices in time.
    ttl = 24 * 3600 if last < now_local().date() else 15 * 60
    rows = dict(await cached(("stroom", first, last), ttl, fetch))
    return [(at, rows[at]) for at in sorted(rows) if first <= datetime.fromtimestamp(at, AMSTERDAM).date() <= last]


def _tax_year(day: date) -> int:
    """The year whose energy tax counts: that of the day itself, or the latest known one when it is missing."""
    return max((year for year in TAX if year <= day.year), default=min(TAX))


def _quarter(at: int, price: float) -> Quarter:
    # Euro per MWh is a tenth of a cent per kWh.
    bare = price / 10
    tax = TAX[_tax_year(datetime.fromtimestamp(at, AMSTERDAM).date())] * 100
    return Quarter(at, bare, (bare + tax) * (1 + BTW))


def _price(quarters: list[Quarter]) -> dict:
    """The average of one or more quarters, in cents per kWh."""
    return {
        "kaal": round(mean(q.kaal for q in quarters), 2),
        "met_belasting": round(mean(q.met_belasting for q in quarters), 2),
    }


def _periods(quarters: list[Quarter]) -> list[str]:
    """Back-to-back quarters as periods, like 13:00-15:30."""
    runs: list[list[Quarter]] = []
    for q in quarters:
        if runs and q.at == runs[-1][-1].at + QUARTER:
            runs[-1].append(q)
        else:
            runs.append([q])
    return [f"{run[0].start:%H:%M}-{run[-1].end:%H:%M}" for run in runs]


def _cheapest(quarters: list[Quarter], size: int) -> dict | None:
    """The `size` back-to-back quarters with the lowest average price; the earliest when two are equal."""
    best: tuple[float, list[Quarter]] | None = None
    for i in range(len(quarters) - size + 1):
        run = quarters[i : i + size]
        if run[-1].at - run[0].at != (size - 1) * QUARTER:
            continue
        average = mean(q.kaal for q in run)
        if best is None or average < best[0]:
            best = (average, run)
    if best is None:
        return None
    run = best[1]
    start = run[0].start
    return {
        "datum": start.date().isoformat(),
        "dag": WEEKDAYS[start.weekday()],
        "van": f"{start:%H:%M}",
        "tot": f"{run[-1].end:%H:%M}",
        **_price(run),
    }


def _day(day: date, quarters: list[Quarter]) -> dict:
    hours: dict[int, dict] = {}
    for q in quarters:
        # By the hour in Unix time, so the hour that comes twice when the clock goes back stays two hours.
        hour = q.at // 3600
        if hour not in hours:
            label = f"{q.start:%H}:00"
            if any(row["uur"] == label for row in hours.values()):
                label += " (wintertijd)"
            hours[hour] = {"uur": label, "kaal": [], "met_belasting": []}
        hours[hour]["kaal"].append(round(q.kaal, 2))
        hours[hour]["met_belasting"].append(round(q.met_belasting, 2))
    low = min(quarters, key=lambda q: q.kaal)
    high = max(quarters, key=lambda q: q.kaal)
    row = {
        "datum": day.isoformat(),
        "dag": WEEKDAYS[day.weekday()],
        "gemiddeld": _price(quarters),
        "laagste": {"tijd": f"{low.start:%H:%M}", **_price([low])},
        "hoogste": {"tijd": f"{high.start:%H:%M}", **_price([high])},
    }
    below_zero = _periods([q for q in quarters if q.kaal < 0])
    if below_zero:
        row["kaal_onder_nul"] = below_zero
    row["per_uur"] = list(hours.values())
    return row


async def stroomprijs(datum: str | None = None) -> dict:
    now = now_local()
    today = now.date()
    tomorrow = today + timedelta(days=1)
    try:
        day = date.fromisoformat(datum.strip()) if datum and datum.strip() else None
    except ValueError as exc:
        raise ToolError(f"Gebruik de datum als JJJJ-MM-DD, bijvoorbeeld {today.isoformat()}.") from exc
    oldest = today - timedelta(days=DAYS_BACK)
    if day is not None and not oldest <= day <= tomorrow:
        raise ToolError(f"Stroomprijzen heb ik van {oldest.isoformat()} tot en met morgen ({tomorrow.isoformat()}).")

    # Today and tomorrow come in one request, shared by everyone; an older day on its own.
    first, last = (today, tomorrow) if day is None or day >= today else (day, day)
    quarters = [_quarter(at, price) for at, price in await _prices(first, last)]
    days = [today, tomorrow] if day is None else [day]
    per_day = {d: [q for q in quarters if q.start.date() == d] for d in days}
    if not any(per_day.values()):
        if day == tomorrow:
            return {"morgen": TOMORROW, "bron": BRON}
        raise ToolError(f"Geen stroomprijzen gevonden voor {(day or today).isoformat()}. Probeer het zo nog eens.")

    result: dict = {"eenheid": "cent per kWh"}
    stamp = now.timestamp()
    current = next((q for q in quarters if q.at <= stamp < q.at + QUARTER), None)
    if current and today in days:
        result["nu"] = {"tijd": f"{current.start:%H:%M}-{current.end:%H:%M}", **_price([current])}
    result["dagen"] = [_day(d, per_day[d]) for d in days if per_day[d]]
    if tomorrow in days and not per_day[tomorrow]:
        result["morgen"] = TOMORROW

    # What is still to come, from the current quarter on; for another day, that whole day.
    from_now = day is None or day == today
    shown = [q for d in days for q in per_day[d]]
    ahead = [q for q in shown if q.at + QUARTER > stamp] if from_now else shown
    cheapest = {name: _cheapest(ahead, size) for name, size in STRETCHES}
    cheapest = {name: stretch for name, stretch in cheapest.items() if stretch}
    if cheapest:
        result["goedkoopst_vanaf_nu" if from_now else "goedkoopst"] = cheapest

    years = sorted({_tax_year(q.start.date()) for q in shown})
    result["energiebelasting"] = [{"jaar": year, "cent_per_kWh_zonder_btw": round(TAX[year] * 100, 3)} for year in years]
    missing = sorted({d.year for d in days if d.year not in TAX})
    if missing:
        result["let_op"] = (
            f"De energiebelasting van {missing[0]} staat er nog niet in, dus de prijs met belasting is gerekend "
            f"met die van {years[-1]}."
        )
    result["uitleg"] = EXPLAIN
    result["bron"] = BRON
    return result

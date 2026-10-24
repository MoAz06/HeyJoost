"""How well the chat answers travel questions, measured: the questions in evals/reis/vragen.json, a few times each,
through the chat's own code (vraag.py), checked against what NS gave the chat for that very answer.

Run from the project folder:
    uv run --no-sync python scripts/eval_reis.py --variant baseline
    uv run --no-sync python scripts/eval_reis.py --variant v1 --vergelijk baseline
Options: --reps 3, --alleen id,id (only these questions), --tegelijk 4, --opnieuw-nakijken (grade the saved answers
again, after a change to the checks), --zelftest (feed the checks a right, an empty and a wrong answer).

A question with trein_over_min is asked at a set time: that many minutes before a train that leaves an hour from now,
so the chat sees a train that leaves right then.

It uses the Anthropic key in .env: one answer is about a third of a cent, so 20 questions 3 times is about $0.20.
Results go to .claude/hillclimb/reis/<variant>/: results.jsonl (a row per answer), raw/ (the answer and what the tools
gave, for grading again), traces/ (the conversation, for the report) and errors.jsonl (answers that never came).
"""

import argparse
import asyncio
import contextvars
import hashlib
import json
import math
import random
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

from nederland_mcp import common, vraag  # noqa: E402
from nederland_mcp.bronnen import ns  # noqa: E402
from nederland_mcp.server import server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "reis" / "vragen.json"
FLOW = ROOT / ".claude" / "hillclimb" / "reis"
# The checks and the questions together: two runs only compare when both were the same.
HARNESS = hashlib.sha256(Path(__file__).read_bytes() + CASES.read_bytes()).hexdigest()[:12]
TIMEOUT = 180
ATTEMPTS = 3
# Haiku 5.5 in dollars per million tokens: input, output, cache write, cache read.
PRICES = {"input_tokens": 0.10, "output_tokens": 0.50, "cache_creation_input_tokens": 0.125, "cache_read_input_tokens": 0.01}

METRICS = [
    ("goed", "Goed"),
    ("zoek", "Zoekactie"),
    ("keuze", "Beste eerst"),
    ("tijden", "Tijden"),
    ("sporen", "Sporen"),
    ("prijzen", "Prijzen"),
    ("overstap", "Overstap"),
    ("toeslag", "Toeslag"),
    ("geen_toeslag", "Geen toeslag"),
    ("anders", "Max 1 extra"),
    ("nu", "Trein van nu"),
    ("rekenen", "Dag en minuten"),
    ("fouten", "Bekende fouten"),
    ("taal", "Taal"),
]
WEEKDAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
MONTHS = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober",
          "november", "december"]
MONTHS_EN = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
             "november", "december"]

TIME = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
SEARCHED = re.compile(r"(vertrek|aankomst|laatste trein) (\d{4}-\d{2}-\d{2})(?: (\d{2}:\d{2}))?")
PLATFORM = re.compile(r"\b(?:spoor|platform|track)\s+(\d+[a-z]?)\b", re.I)
PRICE = re.compile(r"€\s?(\d+)(?:[,.](\d{1,2}))?")
# Things Joost said that NS never told him.
KNOWN = re.compile(
    r"stopt niet|zonder (te )?stoppen|non-?stop|stopt (direct|meteen) (in|op)|rijdt (direct|rechtstreeks) door|"
    r"doesn'?t stop|without stopping",
    re.I,
)
TRANSFER = re.compile(r"overst|over te stappen|\bstap\w*\b[^.]*\bover\b|\bover in\b|transfer|chang", re.I)
# "Op Schiphol stap je over. Je hebt 11 minuten": the minutes can be in a sentence of their own.
HAVE = re.compile(r"\b(?:je hebt|heb je|hebt|you have|you've got|you get)\b[^.]*?\d+\s*min", re.I)
MINUTES = re.compile(r"(\d+)\s*(?:minuten|minuut|minutes?|min)\b", re.I)
# Minutes about something else than the time to change: how long a trip takes, when a train leaves or arrives,
# walking, a margin or a delay.
NOT_CHANGE_BEFORE = re.compile(
    r"(?:\b(?:reis|rit|reistijd|trip|journey|ride)\s+(?:duurt|is|van|takes|of)|\b(?:over|na|binnen|in|within|after)|"
    r"\bdoet\s+er|\b(?:it|that|which)\s+takes)"
    r"\s+(?:ongeveer\s+|ruim\s+|maar\s+|zo'?n\s+|about\s+|just\s+)?$",
    re.I,
)
NOT_CHANGE_AFTER = re.compile(
    r"^\s*(?:lopen|loop|te lopen|marge|later|eerder|vertraging|te laat|onderweg|reistijd|reizen|rijden|in de trein|"
    # "Die duurt 22 minuten langer": how much longer another trip takes.
    r"langer|korter|longer|shorter|"
    r"walk|walking|margin|late|earlier|delay|of travel|travel|voor\s+\d|before\s+\d|"
    # "Je hebt maar 5 minuten over" is the time left before the appointment.
    r"over\b(?!\s+te\s+stappen)|to spare)",
    re.I,
)
# But "overstappen kan in 5 minuten" is about the transfer again. Not past a comma, and not after "zonder overstap".
CHANGE_JUST_BEFORE = re.compile(
    r"(?<!zonder )(?<!geen )(?<!without )(?<!no )(?:overst\w*|over te stappen|\bstap\w* over|transfer|change)"
    r"[^\w,;]+(?:\w+[^\w,;]+){0,4}$",
    re.I,
)
# What a word says about the minutes to change. "Ruim 2 uur", "ruim op tijd" or "liever ruim" is not about a transfer.
LABELS = (
    # "Dus krap is het niet" says the opposite.
    ("krap", re.compile(r"(?<!niet )(?<!not )\b(?:krap|tight)\b(?!,?\s+(?:is|was)\s+(?:het|dat)\s+niet)", re.I),
     lambda gap: gap < 5),
    ("genoeg", re.compile(r"(?<!niet )(?<!not )\b(?:genoeg|enough)\b", re.I), lambda gap: gap >= 5),
    ("ruim", re.compile(r"(?<!liever )\b(?:ruim|plenty)\b(?!\s+(?:\d|op tijd|voor))", re.I), lambda gap: gap >= 10),
)
# Words that say a train leaves right now, or that you only catch it when you are there already.
LEAVING_NOW = re.compile(
    r"\b(?:nu|now|meteen|net|haal\w*|halen|catch|make it|rennen|hurry|opschieten|right away)\b|"
    r"\b(?:over|in) (?:een|1|0|one|a) minu",
    re.I,
)
# The same in a sentence after the one with the train, such as "Die vertrekt nu": only words about leaving now.
NOW_AFTER = re.compile(r"\b(?:nu|now|meteen|right away)\b|\b(?:over|in) (?:about |ongeveer )?(?:een|[0-2]|one|two|a) minu", re.I)
NL_WORDS = set("de het een en je jij is van naar om met die dat niet ook maar dan bij er tot aan nog stap stapt "
               "over vertrekt komt trein".split())
EN_WORDS = set("the a an and you is from to at with that not also but then by there until take arrives departs "
               "leaves train change".split())


class _Failed(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


# What the chat looked up and what Haiku sent back, per answer: each answer runs in its own task.
_seen: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("seen")
# The time of an answer that is asked at a set time.
_clock: contextvars.ContextVar[datetime | None] = contextvars.ContextVar("clock", default=None)
_real_lookup = vraag._lookup


async def _recording_lookup(tools: Any, use: Any, lookups: dict[str, int], visitor: str) -> dict[str, Any]:
    result = await _real_lookup(tools, use, lookups, visitor)
    _seen.get()["lookups"].append(
        {"id": use.id, "name": use.name, "input": use.input, "content": result["content"], "is_error": result["is_error"]}
    )
    return result


class _Round:
    """Keeps every message Haiku sends back, for the model, the tokens and the conversation."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def __aenter__(self) -> Any:
        stream = await self.manager.__aenter__()
        final = stream.get_final_message

        async def get_final_message() -> Any:
            message = await final()
            _seen.get()["rounds"].append(message)
            return message

        stream.get_final_message = get_final_message
        return stream

    async def __aexit__(self, *exc: Any) -> Any:
        return await self.manager.__aexit__(*exc)


def _record() -> None:
    vraag._lookup = _recording_lookup
    client = vraag._anthropic()
    stream = client.messages.stream
    client.messages.stream = lambda *args, **kwargs: _Round(stream(*args, **kwargs))
    # Every part of the chat reads the clock of the answer, when it has one.
    real = common.now_local

    def now_local() -> datetime:
        return _clock.get() or real()

    for module in list(sys.modules.values()):
        if getattr(module, "__name__", "").startswith("nederland_mcp") and getattr(module, "now_local", None) is real:
            module.now_local = now_local


async def _set_clock(case: dict[str, Any]) -> None:
    """For a question about a train that leaves right now: an hour from now NS knows the trains, and the clock goes to
    trein_over_min minutes before the first one."""
    spec = case["zoek"][0]
    later = common.now_local().replace(second=0, microsecond=0) + timedelta(hours=1)
    found = await ns.trein_reis(spec["van"][0], spec["naar"][0], f"{later:%Y-%m-%d}", f"{later:%H:%M}")
    start = later.hour * 60 + later.minute
    ahead = min((_minutes(trip["vertrek"]) - start) % 1440 for trip in found["reisadviezen"])
    _clock.set(later + timedelta(minutes=ahead - case["trein_over_min"]))


async def _ask(case: dict[str, Any], rep: int) -> dict[str, Any]:
    """One answer, the way the page gets it: the chat's own messages, prompt, tools and loop."""
    seen: dict[str, Any] = {"lookups": [], "rounds": []}
    _seen.set(seen)
    if "trein_over_min" in case:
        await _set_clock(case)
    known = {tool.name for tool in await server.list_tools()}
    if vraag.schiphol.available():
        known.add(vraag.FLIGHT_TOOL["name"])
    messages = vraag._messages({"messages": [{"role": "user", "text": case["vraag"]}]}, known)
    english = case.get("taal") == "en"
    started = common.now_local()
    clock = time.monotonic()
    text = ""
    async for event in vraag._answer(server, messages, f"eval-{case['id']}-{rep}", english):
        if event["type"] == "text":
            text += event["text"]
        elif event["type"] == "error":
            raise _Failed("api", event["text"])
    latency = time.monotonic() - clock
    rounds = seen["rounds"]
    if not rounds:
        raise _Failed("harness", "geen antwoord van Haiku opgevangen")
    for message in rounds:
        if not message.model.startswith(vraag.MODEL):
            raise _Failed("model", f"{message.model} in plaats van {vraag.MODEL}")
    usage = {key: sum(getattr(m.usage, key) or 0 for m in rounds) for key in PRICES}
    return {
        "answer": text,
        "lookups": seen["lookups"],
        "started": started.isoformat(),
        "english": english,
        "model": rounds[-1].model,
        "stop_reason": rounds[-1].stop_reason,
        "truncated": any(m.stop_reason == "max_tokens" for m in rounds),
        "usage": usage,
        "latency_s": round(latency, 1),
        "trace": _trace(case, rounds, seen["lookups"], text),
    }


def _trace(case: dict[str, Any], rounds: list[Any], lookups: list[dict[str, Any]], answer: str) -> list[dict[str, Any]]:
    results = {lookup["id"]: lookup for lookup in lookups}
    turns: list[dict[str, Any]] = [
        {"role": "system", "content": vraag._system(vraag.schiphol.available())},
        {"role": "user", "content": case["vraag"]},
    ]
    said = ""
    for message in rounds:
        uses = []
        for block in message.content:
            if block.type == "text" and block.text:
                said += block.text
                turns.append({"role": "assistant", "content": block.text})
            elif block.type == "tool_use":
                uses.append(block)
                turns.append(
                    {"role": "tool_call", "name": block.name, "content": json.dumps(block.input, indent=2, ensure_ascii=False)}
                )
        for use in uses:
            result = results.get(use.id)
            if result:
                turns.append({"role": "tool_result", "name": use.name, "content": _pretty(result["content"])})
    # The chat adds the sources itself when Haiku forgets them.
    if answer.startswith(said) and answer[len(said):].strip():
        turns.append({"role": "assistant", "content": answer[len(said):].strip()})
    return turns


def _pretty(content: str) -> str:
    try:
        return json.dumps(json.loads(content), indent=2, ensure_ascii=False)
    except ValueError:
        return content


# The checks. Each returns (1 or 0, why) or (None, "") when it does not apply to this answer.


def _times(text: str) -> list[str]:
    return [f"{int(hour):02d}:{minute}" for hour, minute in TIME.findall(text)]


def _minutes(clock: str) -> int:
    hour, minute = clock.split(":")
    return int(hour) * 60 + int(minute)


def _calls(lookups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every trein_reis lookup that worked, with what NS was asked."""
    calls = []
    for lookup in lookups:
        if lookup["name"] != "trein_reis" or lookup["is_error"]:
            continue
        try:
            result = json.loads(lookup["content"])
        except ValueError:
            continue
        found = SEARCHED.search(result.get("gezocht_op") or "")
        if not found:
            continue
        kind, day, clock = found.groups()
        # The last train has no time: the tool looks from late in the evening into the night.
        calls.append({"result": result, "aankomst": kind == "aankomst", "dag": date.fromisoformat(day), "tijd": clock or "23:59"})
    return calls


def _route(spec: dict[str, Any], call: dict[str, Any]) -> bool:
    van, naar = (call["result"].get("van") or "").lower(), (call["result"].get("naar") or "").lower()
    return any(name.lower() in van for name in spec["van"]) and any(name.lower() in naar for name in spec["naar"])


def _dates(day: str, today: date) -> set[date]:
    if day == "vandaag":
        return {today}
    if day == "morgen":
        return {today + timedelta(days=1)}
    if day.startswith("volgende week "):
        # The weeks start on Monday: on a Saturday, next week's Tuesday is three days away.
        return {today + timedelta(days=7 - today.weekday() + WEEKDAYS.index(day.split()[-1]))}
    ahead = (WEEKDAYS.index(day) - today.weekday()) % 7
    return {today + timedelta(days=ahead)} if ahead else {today, today + timedelta(days=7)}


def _fits(spec: dict[str, Any], call: dict[str, Any], started: datetime) -> bool:
    want, clock = spec["tijd"], call["tijd"]
    dates = _dates(spec["dag"], started.date())
    if isinstance(want, dict) and clock < "03:00":
        # The last train of the evening can leave after midnight.
        dates |= {day + timedelta(days=1) for day in dates}
    if call["aankomst"] != spec["aankomst"] or call["dag"] not in dates:
        return False
    if want == "nu":
        ahead = (call["dag"] - started.date()).days * 1440 + _minutes(clock) - (started.hour * 60 + started.minute)
        return -5 <= ahead <= 15
    if isinstance(want, str):
        return clock == want
    if isinstance(want, list):
        return want[0] <= clock <= want[1]
    if "na" in want:
        return clock >= want["na"] or clock < "03:00"
    return clock <= want["voor"]


def _expected(spec: dict[str, Any]) -> str:
    want = spec["tijd"]
    if isinstance(want, list):
        want = f"{want[0]}-{want[1]}"
    elif isinstance(want, dict):
        want = f"na {want['na']}" if "na" in want else f"voor {want['voor']}"
    return f"{'aankomst' if spec['aankomst'] else 'vertrek'} {spec['dag']} {want}"


def _check_zoek(case: dict[str, Any], calls: list[dict[str, Any]], started: datetime) -> tuple[int, str]:
    problems = []
    for spec in case["zoek"]:
        route = [call for call in calls if _route(spec, call)]
        if not route:
            problems.append(f"geen zoekactie van {spec['van'][0]} naar {spec['naar'][0]}")
        elif not any(_fits(spec, call, started) for call in route):
            last = route[-1]
            kind = "aankomst" if last["aankomst"] else "vertrek"
            problems.append(f"zocht {kind} {last['dag']} {last['tijd']}, verwacht {_expected(spec)}")
    return (0, "; ".join(problems)) if problems else (1, "")


def _pool(case: dict[str, Any], calls: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The trips of the last search on the asked route, and of all of them."""
    route = [call for call in calls if _route(case["zoek"][0], call)]
    trips = (route[-1]["result"].get("reisadviezen") or []) if route else []
    return trips, [trip for call in route for trip in call["result"].get("reisadviezen") or []]


def _target(case: dict[str, Any], trips: list[dict[str, Any]], pool: list[dict[str, Any]]) -> dict[str, Any] | None:
    if case["keuze"] == "beste":
        return next((trip for trip in trips if trip.get("beste")), trips[0] if trips else None)
    if case["keuze"] == "laatste":
        late = [trip for trip in pool if trip["vertrek"] >= "20:00" or trip["vertrek"] < "03:00"]
        return max(late, key=lambda trip: _minutes(trip["vertrek"]) + 1440 * (trip["vertrek"] < "03:00"), default=None)
    early = [trip for trip in pool if trip["vertrek"] >= "03:00"]
    return min(early, key=lambda trip: _minutes(trip["vertrek"]), default=None)


def _check_keuze(
    case: dict[str, Any], calls: list[dict[str, Any]], answer: str
) -> tuple[int | None, str, dict[str, Any] | None]:
    """Whether the first trip the answer names is the one it should recommend; also returns the trip it names."""
    if not case.get("keuze"):
        return None, "", None
    trips, pool = _pool(case, calls)
    target = _target(case, trips, pool)
    if target is None:
        return 0, "geen passende reis in wat NS gaf", None
    targets = [target]
    if case["keuze"] == "laatste":
        # The last train without a transfer is a fine answer too, such as the last Intercity before a night train.
        direct = [trip for trip in pool if not trip.get("overstappen") and (trip["vertrek"] >= "20:00" or trip["vertrek"] < "03:00")]
        targets += [max(direct, key=lambda trip: _minutes(trip["vertrek"]) + 1440 * (trip["vertrek"] < "03:00"))] if direct else []
    by_clock: dict[str, dict[str, Any]] = {}
    for trip in pool:
        by_clock.setdefault(trip["vertrek"], trip)
    # A late train goes by the time it was meant to leave as well.
    for trip in pool:
        for clock in _departures(trip):
            by_clock.setdefault(clock, trip)
    # A train that leaves right now may come first, as long as the answer says so: _check_nu looks at that.
    named = [clock for clock in _times(answer) if clock in by_clock
             and not (by_clock[clock].get("vertrekt_nu") and by_clock[clock]["vertrek"] != target["vertrek"])]
    if not named:
        return 0, f"noemt geen vertrektijd van een reis, verwacht {target['vertrek']}", target
    first = by_clock[named[0]]
    hit = next((trip for trip in targets if trip["vertrek"] == first["vertrek"]), None)
    if hit is None:
        return 0, f"raadt eerst de {first['vertrek']} aan, verwacht de {target['vertrek']}", first
    return 1, "", hit


def _check_anders(case: dict[str, Any], calls: list[dict[str, Any]], answer: str, chosen: dict[str, Any] | None) -> tuple[int | None, str]:
    # For the first or last train, the one before or after it is part of a good answer.
    if case.get("keuze") != "beste" or chosen is None:
        return None, ""
    _, pool = _pool(case, calls)

    def clocks(trip: dict[str, Any]) -> set[str]:
        found = {trip["vertrek"], trip.get("aankomst")}
        for part in trip.get("delen") or []:
            found |= {part.get("vertrek"), part.get("aankomst")}
        return found

    # A time that belongs to a trip already named, such as its arrival, is not another trip. Nor is the train that
    # leaves right now, which the answer should name.
    explained, others = clocks(chosen), []
    for trip in pool:
        if trip.get("vertrekt_nu"):
            explained |= clocks(trip)
    for clock in _times(answer):
        if clock in explained:
            continue
        trip = next((trip for trip in pool if trip["vertrek"] == clock), None)
        if trip is not None:
            others.append(clock)
            explained |= clocks(trip)
    return (1, "") if len(others) <= 1 else (0, f"noemt {len(others)} andere reizen: {', '.join(others)}")


def _departures(trip: dict[str, Any]) -> set[str]:
    """The times a trip goes by: when it leaves, and when it was meant to."""
    return {trip["vertrek"], *(part["vertrek"] for part in (trip.get("delen") or [])[:1])}


def _check_nu(answer: str, trips: list[dict[str, Any]]) -> tuple[int | None, str]:
    """A train that leaves right now is named, and with it that it leaves now."""
    leaving = [trip for trip in trips if trip.get("vertrekt_nu")]
    if not leaving:
        return None, ""
    sentences = _sentences(answer)
    for trip in leaving:
        own = _departures(trip) | {trip.get("aankomst")} | {part.get(key) for part in trip.get("delen") or []
                                                            for key in ("vertrek", "aankomst")}
        for start, sentence in enumerate(sentences):
            if not _departures(trip) & set(_times(sentence)):
                continue
            if LEAVING_NOW.search(sentence):
                return 1, ""
            # "Er is ook een trein om 21:59. Die vertrekt nu.": the sentences after it, until another train comes up.
            for following in sentences[start + 1:]:
                if not set(_times(following)) <= own:
                    break
                if NOW_AFTER.search(following):
                    return 1, ""
    return 0, f"zegt niet dat de {leaving[0]['vertrek']} nu vertrekt"


def _check_tijden(case: dict[str, Any], answer: str, lookups: list[dict[str, Any]], started: datetime) -> tuple[int | None, str]:
    named = _times(answer)
    if not named:
        return None, ""
    allowed = set(_times(case["vraag"]))
    for lookup in lookups:
        allowed |= set(_times(lookup["content"])) | set(_times(json.dumps(lookup["input"])))
    for hour in map(int, re.findall(r"\b(\d{1,2}) uur\b", case["vraag"])):
        allowed |= {f"{hour % 24:02d}:00", f"{(hour + 12) % 24:02d}:00"}
    for hour in map(int, re.findall(r"\bhalf (\d{1,2})\b", case["vraag"])):
        allowed |= {f"{(hour - 1) % 24:02d}:30", f"{(hour + 11) % 24:02d}:30"}
    allowed |= {(started + timedelta(minutes=shift)).strftime("%H:%M") for shift in range(-2, 3)}
    wrong = list(dict.fromkeys(clock for clock in named if clock not in allowed))
    return (0, f"niet in de gegevens: {', '.join(wrong)}") if wrong else (1, "")


def _check_sporen(answer: str, lookups: list[dict[str, Any]]) -> tuple[int | None, str]:
    named = [platform.lower() for platform in PLATFORM.findall(answer)]
    if not named:
        return None, ""
    allowed = set()
    for lookup in lookups:
        allowed |= {value.lower() for value in re.findall(r'"(?:spoor|perron)": "([^"]+)"', lookup["content"])}
    wrong = list(dict.fromkeys(platform for platform in named if platform not in allowed))
    return (0, f"spoor niet in de gegevens: {', '.join(wrong)}") if wrong else (1, "")


def _check_prijzen(answer: str, lookups: list[dict[str, Any]]) -> tuple[int | None, str]:
    named = [int(euros) + (int(cents.ljust(2, "0")) / 100 if cents else 0) for euros, cents in PRICE.findall(answer)]
    if not named:
        return None, ""
    values = []
    for lookup in lookups:
        values += [float(value) for value in re.findall(r'"(?:prijs|toeslag)\w*": ([\d.]+)', lookup["content"])]
        # NS also writes amounts in its labels, such as "PrijsTijd Deal € 2,22 korting".
        values += [int(euros) + (int(cents.ljust(2, "0")) / 100 if cents else 0)
                   for euros, cents in PRICE.findall(lookup["content"])]
    # A return trip may add up two prices.
    allowed = {round(value, 2) for value in values} | {round(a + b, 2) for a in values for b in values}
    wrong = [price for price in named if round(price, 2) not in allowed]
    return (0, "prijs niet in de gegevens: " + ", ".join(f"€{price:.2f}" for price in wrong)) if wrong else (1, "")


def _sentences(answer: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+|\n+", answer.replace("**", ""))


def _gaps(trip: dict[str, Any]) -> list[int]:
    parts = trip.get("delen") or []
    return [(_minutes(b["vertrek"]) - _minutes(a["aankomst"])) % 1440 for a, b in zip(parts, parts[1:])]


def _allowed_gaps(trip: dict[str, Any]) -> set[int]:
    """The minutes to change in a trip; also right is the time left with the delay of the next train."""
    gaps = _gaps(trip)
    return set(gaps) | {gap + (part.get("vertraging_min") or 0) for gap, part in zip(gaps, (trip.get("delen") or [])[1:])}


def _check_overstap(answer: str, chosen: dict[str, Any] | None, pool: list[dict[str, Any]]) -> tuple[int | None, str]:
    if chosen is None or len(chosen.get("delen") or []) < 2:
        return None, ""
    gaps, own = _gaps(chosen), _allowed_gaps(chosen)
    # The answer may also name the minutes of another trip it mentions.
    allowed = own.union(*(_allowed_gaps(trip) for trip in pool))
    problems, stated, follow = [], [], False
    for sentence in _sentences(answer):
        about = bool(TRANSFER.search(sentence) or HAVE.search(sentence))
        if not (about or follow):
            continue
        numbers, others = [], []
        for match in MINUTES.finditer(sentence):
            before, after, n = sentence[:match.start()], sentence[match.end():], int(match.group(1))
            if NOT_CHANGE_AFTER.search(after) or (NOT_CHANGE_BEFORE.search(before) and not CHANGE_JUST_BEFORE.search(before)):
                others.append((match.start(), n))
                continue
            # More than half an hour, and no trip has that to change: that is how long the trip takes.
            if n > 30 and n not in allowed:
                others.append((match.start(), n))
                continue
            numbers.append((match.start(), n))
        # "Je stapt in Almere over. Dat duurt 5 minuten": the minutes can follow in the next sentence.
        follow = about and not numbers
        if not about and not numbers:
            continue
        stated += [n for _, n in numbers]
        problems += [f"{n} minuten overstappen, NS geeft {'/'.join(map(str, gaps))}" for _, n in numbers if n not in allowed]
        for name, label, fits in LABELS:
            for found in label.finditer(sentence):
                # The word goes with the nearest number, or without one with the only transfer of the trip. Nearest
                # to minutes that are not for changing, as in "dat is krap, je hebt maar 5 minuten over", it goes
                # with those.
                near = min(numbers + others, key=lambda item: abs(item[0] - found.start()), default=None)
                if near in others:
                    continue
                check = [near[1]] if near else gaps if len(gaps) == 1 else []
                if any(not fits(gap) for gap in check):
                    problems.append(f"'{name}' bij {'/'.join(map(str, check))} minuten")
    if not own & set(stated):
        problems.append(f"noemt de overstaptijd niet ({'/'.join(map(str, gaps))} minuten)")
    return (0, "; ".join(dict.fromkeys(problems))) if problems else (1, "")


def _check_geen_toeslag(case: dict[str, Any], calls: list[dict[str, Any]], answer: str) -> tuple[int | None, str]:
    """When someone does not want to pay a supplement: on each route, the first trip the answer names has none."""
    if not case.get("zonder_toeslag"):
        return None, ""
    problems = []
    for spec in case["zoek"]:
        by_clock: dict[str, dict[str, Any]] = {}
        for call in calls:
            if _route(spec, call):
                for trip in call["result"].get("reisadviezen") or []:
                    for clock in _departures(trip):
                        by_clock.setdefault(clock, trip)
        first = next((by_clock[clock] for clock in _times(answer) if clock in by_clock), None)
        if first and first.get("toeslag_euro"):
            problems.append(f"raadt de {first['vertrek']} met toeslag aan")
    return (0, "; ".join(problems)) if problems else (1, "")


def _check_toeslag(answer: str, chosen: dict[str, Any] | None) -> tuple[int | None, str]:
    if chosen is None or not chosen.get("toeslag_euro"):
        return None, ""
    if re.search(r"toeslag|supplement|surcharge", answer, re.I):
        return 1, ""
    return 0, f"noemt de toeslag van €{chosen['toeslag_euro']:.2f} niet"


def _check_rekenen(
    answer: str, calls: list[dict[str, Any]], started: datetime, pool: list[dict[str, Any]], lookups: list[dict[str, Any]]
) -> tuple[int | None, str]:
    """Days and minutes Joost works out himself: morgen, over 3 dagen, maandag 26 oktober, over 4 minuten."""
    # A date or day from a message he looked up, such as works "t/m 22 nov", he did not work out.
    looked_up = " ".join(lookup["content"] for lookup in lookups).lower()
    today = started.date()
    searched = {call["dag"] for call in calls} or {today}
    day = min(searched)
    ahead = (day - today).days
    # "Vluchten kan ik alleen voor gisteren, vandaag of morgen opzoeken", "want vandaag is het al zaterdag" or "de
    # zaterdag na vandaag" is not about the day of the trip.
    low = re.sub(
        r"gisteren,? vandaag (?:of|en) morgen|yesterday,? today,? (?:or|and) tomorrow|"
        r"\bvandaag (?:is het|al)\b|\b(?:het is|is het|na|vanaf) vandaag\b|\btoday is\b|\b\w+day today\b|\bafter today\b",
        " ",
        answer.lower(),
    )
    problems, found = [], False
    words = [
        (r"\b(vandaag|vanavond|vanmiddag|vanochtend|vanmorgen|today|tonight)\b", {0}),
        (r"\bvannacht\b", {0, 1}),
        (r"\b(overmorgen|day after tomorrow)\b", {2}),
        (r"\bmorgen(ochtend|middag|avond|vroeg)?\b", {1}),
        (r"(?<!after )\btomorrow\b", {1}),
        (r"\b(over een week|een week van tevoren|een week vooruit|in a week)\b", {6, 7, 8}),
    ]
    for pattern, allowed in words:
        for match in re.finditer(pattern, low):
            found = True
            if ahead not in allowed:
                problems.append(f"'{match.group(0)}' terwijl het over {ahead} dagen is")
    for match in re.finditer(r"\bover (\d+) dagen\b|\bin (\d+) days\b", low):
        found = True
        if int(match.group(1) or match.group(2)) != ahead:
            problems.append(f"'{match.group(0)}' terwijl het over {ahead} dagen is")
    if re.search(r"\b(volgende week|next week)\b", low):
        found = True
        if day.isocalendar()[1] != (today + timedelta(days=7)).isocalendar()[1]:
            problems.append("'volgende week' klopt niet")
    known_days = searched | {today}
    if any(call["tijd"] >= "20:00" or call["tijd"] < "03:00" for call in calls):
        # A train late in the evening can leave after midnight, on the next day.
        known_days |= {known + timedelta(days=1) for known in searched}
    for number, month in re.findall(r"\b(\d{1,2}) (" + "|".join(MONTHS + MONTHS_EN) + r")\b", low):
        found = True
        index = (MONTHS.index(month) if month in MONTHS else MONTHS_EN.index(month)) + 1
        try:
            named = date(today.year + (index < today.month), index, int(number))
        except ValueError:
            named = None
        if named not in known_days and not re.search(rf"\b{number} {month[:3]}", looked_up):
            problems.append(f"datum {number} {month} klopt niet")
    for name in re.findall(r"\b(" + "|".join(WEEKDAYS) + r")\b", low):
        found = True
        if WEEKDAYS.index(name) not in {known.weekday() for known in known_days} and name not in looked_up:
            problems.append(f"'{name}' klopt niet")
    now = started.hour * 60 + started.minute
    for match in re.finditer(r"\bover (\d+) min|\bin (\d+) min", low):
        found = True
        if ahead != 0:
            continue
        wait = int(match.group(1) or match.group(2))
        if not any(abs((_minutes(trip["vertrek"]) - now) % 1440 - wait) <= 2 for trip in pool):
            problems.append(f"'{match.group(0)}uten' klopt met geen vertrektijd")
    if not found:
        return None, ""
    return (0, "; ".join(dict.fromkeys(problems))) if problems else (1, "")


def _check_fouten(answer: str, chosen: dict[str, Any] | None) -> tuple[int, str]:
    found = KNOWN.search(answer)
    # Unless NS says the train really stops nowhere on the way.
    if found and chosen and all(part.get("tussenstops") == 0 for part in chosen.get("delen") or [{}]):
        return 1, ""
    return (0, f"zegt '{found.group(0)}'") if found else (1, "")


def _check_taal(case: dict[str, Any], answer: str) -> tuple[int, str]:
    words = re.findall(r"[a-z']+", answer.lower())
    nl, en = sum(word in NL_WORDS for word in words), sum(word in EN_WORDS for word in words)
    if case.get("taal") == "en":
        return (1, "") if en > nl else (0, "antwoordt niet in het Engels")
    return (1, "") if nl > en else (0, "antwoordt niet in het Nederlands")


def grade(case: dict[str, Any], raw: dict[str, Any]) -> tuple[dict[str, float], dict[str, str]]:
    answer, lookups = raw["answer"], raw["lookups"]
    started = datetime.fromisoformat(raw["started"])
    calls = _calls(lookups)
    keuze, keuze_why, chosen = _check_keuze(case, calls, answer)
    trips, pool = _pool(case, calls)
    checks = {
        "zoek": _check_zoek(case, calls, started),
        "keuze": (keuze, keuze_why),
        "tijden": _check_tijden(case, answer, lookups, started),
        "sporen": _check_sporen(answer, lookups),
        "prijzen": _check_prijzen(answer, lookups),
        "overstap": _check_overstap(answer, chosen, pool),
        "toeslag": _check_toeslag(answer, chosen),
        "geen_toeslag": _check_geen_toeslag(case, calls, answer),
        "anders": _check_anders(case, calls, answer, chosen),
        "nu": _check_nu(answer, trips),
        "rekenen": _check_rekenen(answer, calls, started, pool, lookups),
        "fouten": _check_fouten(answer, chosen),
        "taal": _check_taal(case, answer),
    }
    scores = {name: float(score) for name, (score, _) in checks.items() if score is not None}
    why = {name: reason for name, (score, reason) in checks.items() if score == 0 and reason}
    scores = {"goed": float(all(score == 1 for score in scores.values())), **scores}
    return scores, why


# Running, saving and summing up.


def _append(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(row, ensure_ascii=False) + "\n")


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _row(case: dict[str, Any], rep: int, raw: dict[str, Any]) -> dict[str, Any]:
    scores, why = grade(case, raw)
    return {
        "prompt_id": case["id"],
        "prompt": case["vraag"],
        "tags": case["tags"],
        "rep": rep,
        "status": "truncated" if raw["truncated"] else "ok",
        "stop_reason": raw["stop_reason"],
        "grade": scores,
        "explanation": why,
        "model": raw["model"],
        "usage": raw["usage"],
        "latency_s": raw["latency_s"],
        "tool_calls": len(raw["lookups"]),
        "harness": HARNESS,
        "meta": {"antwoord": raw["answer"], "gezocht": [{"tool": l["name"], "input": l["input"]} for l in raw["lookups"]]},
    }


async def _one(case: dict[str, Any], rep: int, out: Path, semaphore: asyncio.Semaphore) -> None:
    async with semaphore:
        for attempt in range(1, ATTEMPTS + 1):
            failure: dict[str, Any] = {"prompt_id": case["id"], "rep": rep, "attempt": attempt}
            try:
                raw = await asyncio.wait_for(_ask(case, rep), TIMEOUT)
            except _Failed as exc:
                _append(out / "errors.jsonl", {**failure, "class": exc.kind, "detail": exc.detail})
                if exc.kind == "model":
                    return
            except asyncio.TimeoutError:
                _append(out / "errors.jsonl", {**failure, "class": "timeout", "detail": f"meer dan {TIMEOUT} s"})
            except Exception as exc:  # noqa: BLE001 - a broken case must not stop the others
                _append(out / "errors.jsonl", {**failure, "class": "harness", "detail": repr(exc)[:500]})
            else:
                name = f"{case['id']}_rep{rep}.json"
                trace = raw.pop("trace")
                (out / "raw" / name).write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
                (out / "traces" / name).write_text(json.dumps(trace, ensure_ascii=False, indent=1), encoding="utf-8")
                row = _row(case, rep, raw)
                _append(out / "results.jsonl", row)
                mark = "goed" if row["grade"]["goed"] else "fout: " + "; ".join(row["explanation"].values())
                print(f"  {case['id']} #{rep}: {mark}", flush=True)
                return
            # Wait a little longer each time, and not all at once.
            await asyncio.sleep(random.uniform(2, 6) * attempt)


def _cost(usage: dict[str, int]) -> float:
    return sum(usage.get(key, 0) * price for key, price in PRICES.items()) / 1_000_000


def _rates(rows: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    """Per check: how many answers passed, of the answers it applied to."""
    rates = {}
    for metric, _ in METRICS:
        scored = [row["grade"][metric] for row in rows if row["status"] == "ok" and metric in row["grade"]]
        rates[metric] = (int(sum(scored)), len(scored))
    return rates


def _headline(rows: list[dict[str, Any]]) -> tuple[float, float, int]:
    """Mean of 'goed' per question over its answers, then over the questions, with a 95% interval."""
    per_case: dict[str, list[float]] = {}
    for row in rows:
        if row["status"] == "ok":
            per_case.setdefault(row["prompt_id"], []).append(row["grade"]["goed"])
    means = [sum(scores) / len(scores) for scores in per_case.values()]
    if not means:
        return 0.0, 0.0, 0
    mean = sum(means) / len(means)
    spread = math.sqrt(sum((m - mean) ** 2 for m in means) / max(1, len(means) - 1))
    return mean, 1.96 * spread / math.sqrt(len(means)), len(means)


def _summary(variant: str) -> None:
    out = FLOW / variant
    rows, errors = _rows(out / "results.jsonl"), _rows(out / "errors.jsonl")
    mean, margin, cases = _headline(rows)
    cost = sum(_cost(row["usage"]) for row in rows)
    print(f"\n{variant}: {len(rows)} antwoorden op {cases} vragen, goed {mean:.0%} ± {margin:.0%}")
    for metric, label in METRICS:
        passed, scored = _rates(rows)[metric]
        if scored:
            print(f"  {label:<15} {passed:>3}/{scored:<3} {passed / scored:>5.0%}")
    truncated = sum(row["status"] != "ok" for row in rows)
    print(f"  kosten ${cost:.3f}, gemiddeld {sum(r['latency_s'] for r in rows) / max(1, len(rows)):.0f} s per antwoord, "
          f"{len(errors)} mislukte pogingen, {truncated} afgekapt")


def _compare(base: str, variant: str) -> None:
    left, right = _rows(FLOW / base / "results.jsonl"), _rows(FLOW / variant / "results.jsonl")
    harnesses = {row["harness"] for row in left + right}
    if len(harnesses) > 1:
        sys.exit(f"De checks of vragen verschillen tussen {base} en {variant}; kijk eerst allebei opnieuw na.")
    print(f"\n{'':<15} {base:>14} {variant:>14}")
    for metric, label in METRICS:
        a, b = _rates(left)[metric], _rates(right)[metric]
        if a[1] or b[1]:
            print(f"{label:<15} {a[0]:>5}/{a[1]:<3}{a[0] / max(1, a[1]):>5.0%} {b[0]:>5}/{b[1]:<3}{b[0] / max(1, b[1]):>5.0%}")
    # Per question, the change in 'goed', and a 95% interval over the questions.
    def per_case(rows: list[dict[str, Any]]) -> dict[str, float]:
        scores: dict[str, list[float]] = {}
        for row in rows:
            if row["status"] == "ok":
                scores.setdefault(row["prompt_id"], []).append(row["grade"]["goed"])
        return {case: sum(s) / len(s) for case, s in scores.items()}
    a, b = per_case(left), per_case(right)
    both = sorted(set(a) & set(b))
    deltas = [b[case] - a[case] for case in both]
    if deltas:
        mean = sum(deltas) / len(deltas)
        spread = math.sqrt(sum((d - mean) ** 2 for d in deltas) / max(1, len(deltas) - 1))
        margin = 1.96 * spread / math.sqrt(len(deltas))
        verdict = "echt verschil" if abs(mean) > margin else "binnen de ruis"
        print(f"\nGoed per vraag: {mean:+.0%} ± {margin:.0%} ({verdict}, {len(deltas)} vragen)")
        for case in both:
            if b[case] != a[case]:
                print(f"  {case}: {a[case]:.0%} -> {b[case]:.0%}")


def _regrade(variant: str, cases: dict[str, dict[str, Any]]) -> None:
    out = FLOW / variant
    rows = []
    for row in _rows(out / "results.jsonl"):
        raw = json.loads((out / "raw" / f"{row['prompt_id']}_rep{row['rep']}.json").read_text(encoding="utf-8"))
        rows.append(_row(cases[row["prompt_id"]], row["rep"], raw))
    (out / "results.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(f"{len(rows)} antwoorden van {variant} opnieuw nagekeken")


def _selftest(variant: str, cases: dict[str, dict[str, Any]]) -> None:
    """The checks on answers whose grade is known: a right one, an empty one and a wrong one."""
    out = FLOW / variant
    results = {"goed antwoord": [], "leeg antwoord": [], "fout antwoord": []}
    for path in sorted((out / "raw").glob("*.json")):
        case = cases[path.stem.rsplit("_rep", 1)[0]]
        raw = json.loads(path.read_text(encoding="utf-8"))
        calls = _calls(raw["lookups"])
        trips, pool = _pool(case, calls)
        target = _target(case, trips, pool) if case.get("keuze") else None
        if target is None:
            continue
        english = case.get("taal") == "en"
        first = target["delen"][0]
        if english:
            right = f"Take the **{target['vertrek']}** from platform {first['spoor']}. You arrive at **{target['aankomst']}**."
        else:
            right = f"Neem de **{target['vertrek']}** van spoor {first['spoor']}. Je bent er om **{target['aankomst']}**."
        for gap in _gaps(target):
            word = ("tight" if gap < 5 else "enough" if gap < 10 else "plenty") if english else (
                "krap" if gap < 5 else "genoeg" if gap < 10 else "ruim")
            right += (f" You have {gap} minutes to change, that is {word}." if english
                      else f" Je hebt {gap} minuten om over te stappen, dat is {word}.")
        if target.get("toeslag_euro"):
            right += (f" The price includes a supplement of €{target['toeslag_euro']:.2f}." if english
                      else f" Er zit €{target['toeslag_euro']:.2f} toeslag in de prijs.".replace(".", ",", 1))
        # Also when the best itself leaves now: then that is the one to say it of.
        for trip in trips:
            if trip.get("vertrekt_nu"):
                right += (f" The {trip['vertrek']} leaves now, you only catch it if you are at the train already."
                          if english else f" De trein van {trip['vertrek']} vertrekt nu, die haal je alleen als je al "
                          "bij de trein staat.")
        other = next((trip for trip in pool if trip["vertrek"] != target["vertrek"]), None)
        wrong = f"Neem de **{other['vertrek'] if other else '12:34'}**, of die van **12:34**, van spoor 99. Die stopt niet onderweg."
        for label, answer in (("goed antwoord", right), ("leeg antwoord", "Dat weet ik niet."), ("fout antwoord", wrong)):
            scores, why = grade(case, {**raw, "answer": answer})
            # The search is what Haiku did, not the answer, so it is left out here.
            scores.pop("zoek", None)
            passed = all(score == 1 for name, score in scores.items() if name != "goed")
            results[label].append((path.stem, passed, why))
    for label, outcomes in results.items():
        passed = sum(ok for _, ok, _ in outcomes)
        print(f"{label}: {passed}/{len(outcomes)} door alle checks")
        if label == "goed antwoord":
            for stem, ok, why in outcomes:
                if not ok:
                    print(f"  {stem}: {why}")


async def _run(args: argparse.Namespace, cases: dict[str, dict[str, Any]]) -> None:
    out = FLOW / args.variant
    for folder in (out / "raw", out / "traces"):
        folder.mkdir(parents=True, exist_ok=True)
    state = FLOW / "_state.json"
    if not state.exists():
        metrics = [{"id": metric, "label": label, "kind": "binary"} for metric, label in METRICS]
        state.write_text(json.dumps({"metrics": metrics}, ensure_ascii=False, indent=1), encoding="utf-8")
    done = {(row["prompt_id"], row["rep"]) for row in _rows(out / "results.jsonl")}
    chosen = args.alleen.split(",") if args.alleen else list(cases)
    todo = [(cases[case], rep) for case in chosen for rep in range(args.reps) if (case, rep) not in done]
    print(f"{args.variant}: {len(todo)} antwoorden te gaan ({len(done)} al klaar), checks {HARNESS}")
    _record()
    semaphore = asyncio.Semaphore(args.tegelijk)
    clock = time.monotonic()
    await asyncio.gather(*(_one(case, rep, out, semaphore) for case, rep in todo))
    print(f"klaar in {time.monotonic() - clock:.0f} s")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Meet hoe goed de chat reisvragen beantwoordt.")
    parser.add_argument("--variant", default="baseline", help="baseline, v1, v2, ...")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--alleen", help="alleen deze vragen, met komma's")
    parser.add_argument("--tegelijk", type=int, default=4)
    parser.add_argument("--vergelijk", help="vergelijk met deze variant")
    parser.add_argument("--opnieuw-nakijken", action="store_true")
    parser.add_argument("--zelftest", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"baseline|v\d+", args.variant):
        sys.exit("Een variant heet baseline, v1, v2, ...")
    cases = {case["id"]: case for case in json.loads(CASES.read_text(encoding="utf-8"))}
    if args.zelftest:
        _selftest(args.variant, cases)
        return
    if args.opnieuw_nakijken:
        _regrade(args.variant, cases)
    else:
        asyncio.run(_run(args, cases))
    _summary(args.variant)
    if args.vergelijk:
        _compare(args.vergelijk, args.variant)


if __name__ == "__main__":
    main()

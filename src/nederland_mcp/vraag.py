"""Vraag het Joost: the chat on the website, which answers with the Hey Joost tools through Claude.

The page sends the conversation so far and the server keeps nothing. A daily budget and a limit per
visitor keep a busy day from eating the API credits.
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import anthropic
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from . import tellers
from .bronnen import schiphol
from .common import now_local

log = logging.getLogger(__name__)
# The Anthropic client logs every request; the requests say nothing new, so keep them out.
logging.getLogger("httpx2").setLevel(logging.WARNING)

MODEL = "claude-haiku-5-5"
# Dollars per million tokens for Haiku 5.5, to keep track of the daily budget.
PRICE_INPUT, PRICE_OUTPUT, PRICE_CACHE_WRITE, PRICE_CACHE_READ = 0.10, 0.50, 0.125, 0.01
# Booked before each call and settled after it, so a visitor who leaves halfway still counts.
ROUND_RESERVE = 0.002
# Haiku thinks before it answers, and that counts as output too: 1024 cut off some answers halfway.
MAX_TOKENS = 2048
# Low was not faster in tests, and high not better.
EFFORT = "medium"
MAX_ROUNDS = 5
MAX_LOOKUPS = 6
# Transitous was told a few calls per question, so one question looks up at most three stops.
MAX_STOP_LOOKUPS = 3
# Flights only exist in this chat, with the limits agreed with Schiphol (see bronnen/schiphol.py).
MAX_FLIGHT_LOOKUPS = 2
MAX_QUESTION = 500
MAX_REPLY = 4000
MAX_MESSAGES = 12
MAX_BODY = 32_000
MAX_BUSY = 8
BUSY_TIMEOUT = 120
STREAM_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}

OFF = "Vraag het Joost staat nu uit. Zet Joost in je eigen AI, dan kan het altijd."
TIRED = "Joost is even moe, de vragen voor vandaag zijn op. Morgen weer! Of zet Joost in je eigen AI, dan kan het altijd."
CROWDED = "Het is even druk bij Joost. Probeer het zo nog eens."
STILL_BUSY = "Joost is nog bezig met je vorige vraag."
TOO_LONG = f"Je vraag is te lang. Hou hem onder de {MAX_QUESTION} tekens."
CONFUSED = "Dat snapte Joost niet. Ververs de pagina en probeer het nog eens."
UNREACHABLE = "Joost is even niet bereikbaar. Probeer het zo nog eens."
REFUSED = "Daar kan Joost niet mee helpen."
# For Claude, not the visitor: without "not looked up" it reports a skipped lookup as a failed one.
ENOUGH = (
    "Niet opgezocht: dit is het maximum voor één vraag, het is dus niet mislukt. Antwoord met wat je al weet, "
    "zeg eerlijk wat je niet hebt opgezocht en dat de gebruiker dat apart kan vragen."
)
FLIGHTS_DONE = (
    "Niet opgezocht: deze bezoeker heeft vandaag al het maximum aan vluchten opgezocht, het is dus niet mislukt. "
    "Zeg dat vluchten morgen weer kunnen, en verwijs voor nu naar schiphol.nl of de app van de luchtvaartmaatschappij."
)
FLIGHTS_FULL = (
    "Niet opgezocht: het maximum aan vluchten voor vandaag is bereikt, voor de hele site, het is dus niet mislukt. "
    "Zeg dat vluchten morgen weer kunnen, en verwijs voor nu naar schiphol.nl of de app van de luchtvaartmaatschappij."
)
FAILED = "Dit lukte even niet bij de bron. Probeer het later nog eens."

# Shown while Joost looks something up: "Hey Joost kijkt bij de NS".
WHERE = {
    "trein_vertrek": "bij de NS",
    "trein_aankomst": "bij de NS",
    "trein_reis": "bij de NS",
    "ov_vertrek": "bij de vervoerders",
    "water_actueel": "bij Rijkswaterstaat",
    "schoolvakanties": "bij de Rijksoverheid",
    "huis_info": "in de BAG",
    "vlucht_info": "bij Schiphol",
    "weer": "bij Open-Meteo (KNMI)",
}
# Named in every answer that uses their data: Schiphol as promised, Open-Meteo for its licence (CC BY 4.0).
# The word to look for in the answer, and what to add when Claude left it out.
CREDITS = {"vlucht_info": ("schiphol", "Schiphol"), "weer": ("open-meteo", "KNMI via Open-Meteo.com")}
# Where a question came from, as the page reports it. Only counted, never stored with anything else.
ORIGINS = ("getypt", "voorbeeld", "vast", "link")

# Not an MCP tool: Schiphol only allows flights in this chat. Only offered when the server has a Schiphol key.
FLIGHT_TOOL = {
    "name": "vlucht_info",
    "description": (
        "Status van één vlucht van of naar Schiphol: geplande, verwachte en werkelijke tijd, status, terminal, "
        "gate en bagageband. Eén vluchtnummer per keer, alleen voor gisteren, vandaag of morgen."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "vluchtnummer": {"type": "string", "description": "Vluchtnummer, bijvoorbeeld KL1001 of HV5131"},
            "datum": {"type": "string", "description": "Datum als JJJJ-MM-DD: gisteren, vandaag of morgen. Leeg = vandaag"},
        },
        "required": ["vluchtnummer"],
    },
}

SYSTEM = """Je bent Joost, de assistent op heyjoost.nl. Je bent een AI (Claude, van Anthropic). Vraagt iemand of je een mens bent, dan zeg je eerlijk dat je een AI bent.

Je helpt met live informatie over Nederland, via je tools: treinen van de NS (vertrek, aankomst en reisadvies met prijs en drukte), bus, tram, metro en veerboot, {vluchten}het weer (temperatuur, regen en wind, tot een week vooruit), waterstand en watertemperatuur, schoolvakanties, en feiten over adressen en gebouwen uit de BAG.

Zo antwoord je:
- Zoek actuele feiten altijd op met een tool. Verzin nooit tijden, sporen, perrons, vertragingen of prijzen.
- Roep een tool meteen aan, zonder eerst te zeggen dat je gaat zoeken.
- Hou het kort: een paar zinnen, of een kort lijstje met "- ". Geen tabellen of kopjes. Zet tijden, sporen en perrons vet met **.
- Sluit af met de bron in een paar woorden, zoals "Bron: NS". De bron staat in het veld 'bron' van elk resultaat.
- Lukt iets niet of vind je niks, zeg dat dan eerlijk en stel een betere vraag voor.
- Mis je iets wat echt nodig is, zoals de plaats van een halte, vraag dat dan kort.
- Een routeplanner heb je niet, dus van deur tot deur doe je in stukjes. Moet iemand vanaf een adres met de trein naar een andere plaats, zoek dan tegelijk de trein met trein_reis vanaf het grote station in de buurt, en met ov_vertrek de bus of tram vanaf het adres of de straat met naar dat station, dus niet naar de eindbestemming. Noem de halte waar je opstapt en hoe ver die ongeveer is. De afstand is hemelsbreed, lopen is iets verder.
- Gaat het om een later moment, geef dan datum en tijd mee aan de tool.
- Zeg nooit dat iets niet lukte als je het niet hebt geprobeerd.
- Je eerdere antwoorden in dit gesprek heb je echt opgezocht, ook al zie je die zoekacties niet meer. Kom er dus niet op terug en zoek ze niet opnieuw op, behalve als de gebruiker daarom vraagt.
- Antwoord in de taal van de vraag. In het Nederlands schrijf je informeel, met je en jij.
- Gaat het om iets belangrijks, zoals een trein halen, zeg dan dat de officiële app de laatste stand heeft.

Grenzen:
- Je helpt alleen met deze onderwerpen. Vraagt iemand iets anders, zoals huiswerk, code, nieuws of een verhaal, zeg dan vriendelijk dat je hier alleen live info over Nederland geeft, en dat iemand Hey Joost ook in een eigen AI kan zetten, zoals Claude of ChatGPT. Hoe dat moet, staat op deze pagina.
{vluchtgrenzen}
- Het weer is een verwachting uit weermodellen. Weerwaarschuwingen heb je niet: bij zwaar weer verwijs je naar knmi.nl. Noem bij het weer als bron precies "KNMI via Open-Meteo.com".
- Zoek geen mensen op en vraag niet naar persoonsgegevens. Een adres gebruik je alleen voor feiten over het gebouw of de haltes in de buurt. Voor haltes is een straat en plaats genoeg, dus vraag niet om een huisnummer of postcode.
- Gaat het over een noodgeval, verwijs dan naar 112."""
# Filled in by _system(), with or without flights.
FLIGHT_RULES = (
    "- Vluchten zoek je op bij Schiphol: één vluchtnummer per keer, en alleen voor gisteren, vandaag of morgen. "
    "Lijsten met vluchten, of vluchten op andere dagen, heb je niet. Verwijs daarvoor naar schiphol.nl.\n"
    '- Noem bij vluchtgegevens altijd Schiphol als bron, zoals "Bron: Schiphol".'
)
NO_FLIGHTS = (
    "- Vluchtinformatie heb je niet. Vraagt iemand naar een vlucht, verwijs dan naar schiphol.nl of de app "
    "van de luchtvaartmaatschappij."
)

DAYS = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")
MONTHS = (
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
)


class _Day:
    """Today's spending, and questions and flights per visitor. In memory, so a restart starts the count over."""

    def __init__(self) -> None:
        self.date: date | None = None
        self.spent = 0.0
        self.asked: dict[str, int] = {}
        self.flights: dict[str, int] = {}
        self.all_flights = 0

    def today(self) -> "_Day":
        current = now_local().date()
        if current != self.date:
            self.date, self.spent, self.asked = current, 0.0, {}
            self.flights, self.all_flights = {}, 0
        return self


_day = _Day()
_busy: dict[str, float] = {}
# Visitors are counted by a salted hash of their address, so the address itself is never kept.
_salt = secrets.token_bytes(16)
_client: anthropic.AsyncAnthropic | None = None


def _anthropic() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(timeout=60.0, max_retries=1)
    return _client


def _budget() -> float:
    return float(os.environ.get("VRAAG_DAGBUDGET_USD") or 0.5)


def _per_day() -> int:
    return int(os.environ.get("VRAAG_PER_DAG") or 30)


def _flights_per_day() -> int:
    return int(os.environ.get("VLUCHT_PER_DAG") or 10)


def _flights_per_site() -> int:
    return int(os.environ.get("VLUCHT_SITE_PER_DAG") or 300)


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and _day.today().spent < _budget()


def flights() -> bool:
    """Whether the chat can look up flights now, so the home page only mentions them then."""
    return available() and schiphol.available() and _day.today().all_flights < _flights_per_site()


async def ask(request: Request, tools: MCPServer) -> Response:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _refuse(503, OFF)
    origin = request.headers.get("origin")
    if origin and origin.partition("://")[2] != request.headers.get("host"):
        return _refuse(403, CONFUSED)

    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            return _refuse(413, TOO_LONG)
    try:
        data = json.loads(body)
        messages = _messages(data)
    except ValueError as exc:
        return _refuse(400, str(exc) if str(exc) in (TOO_LONG, CONFUSED) else CONFUSED)

    day = _day.today()
    if day.spent >= _budget():
        return _refuse(429, TIRED)
    visitor = _visitor(request)
    now = time.monotonic()
    for key, started in list(_busy.items()):
        if now - started > BUSY_TIMEOUT:
            del _busy[key]
    if visitor in _busy:
        return _refuse(429, STILL_BUSY)
    if len(_busy) >= MAX_BUSY:
        return _refuse(503, CROWDED)
    if day.asked.get(visitor, 0) >= _per_day():
        return _refuse(429, f"Je hebt vandaag al {_per_day()} vragen gesteld. Morgen weer! Of zet Joost in je eigen AI, dan kan het altijd.")
    if visitor not in day.asked:
        tellers.tel("chat bezoeker")
    day.asked[visitor] = day.asked.get(visitor, 0) + 1
    _busy[visitor] = now
    tellers.tel("chat")
    if data.get("via") in ORIGINS:
        tellers.tel(f"chat via {data['via']}")
    if data.get("app") is True:
        tellers.tel("chat in app")
    return StreamingResponse(_events(tools, messages, visitor), media_type="text/event-stream", headers=STREAM_HEADERS)


def _refuse(status: int, text: str) -> JSONResponse:
    return JSONResponse({"error": text}, status_code=status)


def _visitor(request: Request) -> str:
    # Behind Cloudflare the visitor's address is in cf-connecting-ip. It can be faked by skipping
    # Cloudflare, which only buys more questions per day: the daily budget still holds.
    address = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not address and request.client:
        address = request.client.host
    return hashlib.sha256(_salt + address.encode()).hexdigest()[:16]


def _messages(data: Any) -> list[dict[str, Any]]:
    """The conversation from the page as API messages: text only, taking turns, ending with the question."""
    items = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError(CONFUSED)
    messages: list[dict[str, Any]] = []
    for item in items[-MAX_MESSAGES:]:
        role = item.get("role") if isinstance(item, dict) else None
        text = item.get("text") if isinstance(item, dict) else None
        if role not in ("user", "assistant") or not isinstance(text, str):
            raise ValueError(CONFUSED)
        text = text.strip()
        if item is items[-1] and len(text) > MAX_QUESTION:
            raise ValueError(TOO_LONG)
        text = text[:MAX_REPLY]
        if not text:
            continue
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n\n" + text
        else:
            messages.append({"role": role, "content": text})
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    if not messages or messages[-1]["role"] != "user":
        raise ValueError(CONFUSED)
    return messages


async def _events(tools: MCPServer, messages: list[dict[str, Any]], visitor: str) -> AsyncIterator[str]:
    try:
        async for event in _answer(tools, messages, visitor):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    finally:
        _busy.pop(visitor, None)


async def _answer(tools: MCPServer, messages: list[dict[str, Any]], visitor: str) -> AsyncIterator[dict[str, str]]:
    day = _day.today()
    definitions = [
        {"name": tool.name, "description": tool.description or "", "input_schema": tool.input_schema}
        for tool in await tools.list_tools()
    ]
    with_flights = schiphol.available()
    if with_flights:
        definitions.append(FLIGHT_TOOL)
    # The fixed part comes first and is cached; the clock changes every minute.
    system = [
        {"type": "text", "text": _system(with_flights), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _now()},
    ]
    lookups: dict[str, int] = {}
    said, used = "", set()
    cost = 0.0
    for round_ in range(MAX_ROUNDS):
        day.spent += ROUND_RESERVE
        try:
            async with _anthropic().messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                output_config={"effort": EFFORT},
                system=system,
                tools=definitions,
                # The last round answers with what it found so far.
                tool_choice={"type": "none" if round_ == MAX_ROUNDS - 1 else "auto"},
                messages=messages,
            ) as stream:
                async for event in stream:
                    if event.type == "text":
                        said += event.text
                        yield {"type": "text", "text": event.text}
                    elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                        yield {"type": "tool", "where": WHERE.get(event.content_block.name, "bij de bron")}
                message = await stream.get_final_message()
        except anthropic.APIError as exc:
            day.spent -= ROUND_RESERVE
            yield {"type": "error", "text": _api_error(exc, day)}
            return
        spent = _cost(message.usage)
        day.spent += spent - ROUND_RESERVE
        cost += spent
        if message.stop_reason == "refusal":
            yield {"type": "error", "text": REFUSED}
            return
        uses = [block for block in message.content if block.type == "tool_use"]
        if message.stop_reason != "tool_use" or not uses:
            break
        blocks = [_param(block) for block in message.content if block.type != "text" or block.text]
        results = await asyncio.gather(*(_lookup(tools, use, lookups, visitor) for use in uses))
        used.update(use.name for use, result in zip(uses, results) if not result["is_error"])
        messages = [*messages, {"role": "assistant", "content": blocks}, {"role": "user", "content": results}]
    if message.stop_reason == "max_tokens":
        yield {"type": "text", "text": " …"}
    missing = [credit for name, (word, credit) in CREDITS.items() if name in used and word not in said.lower()]
    if missing:
        # Even if Claude forgets, the answer names these sources.
        yield {"type": "text", "text": "\n\nBron: " + ", ".join(missing)}
    log.info(
        "Vraag het Joost: %d rondes, %d opzoekingen, $%.4f, vandaag $%.3f van $%.2f",
        round_ + 1, sum(lookups.values()), cost, day.spent, _budget(),
    )
    yield {"type": "done"}


def _param(block: Any) -> dict[str, Any]:
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    # Haiku thinks before it answers; that thinking goes back with the tool results, so it can carry on with it.
    if block.type == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if block.type == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    return {"type": "text", "text": block.text}


async def _lookup(tools: MCPServer, use: Any, lookups: dict[str, int], visitor: str) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "tool_result", "tool_use_id": use.id}
    # Each lookup runs in its own task, so this only marks the chat's own calls for the counts.
    tellers.via.set("chat")
    lookups[use.name] = lookups.get(use.name, 0) + 1
    if (
        sum(lookups.values()) > MAX_LOOKUPS
        or lookups.get("ov_vertrek", 0) > MAX_STOP_LOOKUPS
        or lookups.get(FLIGHT_TOOL["name"], 0) > MAX_FLIGHT_LOOKUPS
    ):
        return {**result, "content": ENOUGH, "is_error": True}
    try:
        if use.name == FLIGHT_TOOL["name"]:
            return {**result, "content": await _flight(use.input, visitor), "is_error": False}
        output = await tools.call_tool(use.name, use.input)
    except UnexpectedToolError as exc:
        log.warning("Vraag het Joost: %s faalde (%s)", use.name, type(exc.__cause__).__name__)
        return {**result, "content": FAILED, "is_error": True}
    except ToolError as exc:
        return {**result, "content": str(exc), "is_error": True}
    except Exception as exc:
        log.warning("Vraag het Joost: %s faalde (%s)", use.name, type(exc).__name__)
        return {**result, "content": FAILED, "is_error": True}
    data = getattr(output, "structured_content", None)
    if data is None:
        text = " ".join(getattr(part, "text", "") for part in getattr(output, "content", []))
    else:
        text = json.dumps(data, ensure_ascii=False)
    return {**result, "content": text or "Geen resultaat.", "is_error": bool(getattr(output, "is_error", False))}


async def _flight(question: Any, visitor: str) -> str:
    """One flight from Schiphol, within the limits per visitor and for the whole site."""
    number = question.get("vluchtnummer") if isinstance(question, dict) else None
    datum = question.get("datum") if isinstance(question, dict) else None
    if not isinstance(number, str) or not isinstance(datum, str | None):
        raise ToolError("Geef een vluchtnummer, zoals KL1001, en eventueel een datum als JJJJ-MM-DD.")
    # Checked before counting, so a wrong number or a day outside the window costs no lookup.
    schiphol.flight(number, datum)
    day = _day.today()
    if day.all_flights >= _flights_per_site():
        raise ToolError(FLIGHTS_FULL)
    if day.flights.get(visitor, 0) >= _flights_per_day():
        raise ToolError(FLIGHTS_DONE)
    day.all_flights += 1
    day.flights[visitor] = day.flights.get(visitor, 0) + 1
    tellers.tool(FLIGHT_TOOL["name"])
    return json.dumps(await schiphol.vlucht_info(number, datum), ensure_ascii=False)


def _api_error(exc: anthropic.APIError, day: _Day) -> str:
    log.warning("Vraag het Joost: %s %s %s", type(exc).__name__, getattr(exc, "status_code", ""), str(exc)[:200])
    if isinstance(exc, anthropic.BadRequestError) and "credit balance" in str(exc):
        # Out of credits: stop asking for today, which also hides the chat on the home page.
        day.spent = max(day.spent, _budget())
        return TIRED
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return OFF
    if isinstance(exc, anthropic.RateLimitError):
        return CROWDED
    return UNREACHABLE


def _cost(usage: Any) -> float:
    return (
        usage.input_tokens * PRICE_INPUT
        + usage.output_tokens * PRICE_OUTPUT
        + (usage.cache_creation_input_tokens or 0) * PRICE_CACHE_WRITE
        + (usage.cache_read_input_tokens or 0) * PRICE_CACHE_READ
    ) / 1_000_000


def _system(with_flights: bool) -> str:
    return SYSTEM.replace("{vluchten}", "vluchten van en naar Schiphol, " if with_flights else "").replace(
        "{vluchtgrenzen}", FLIGHT_RULES if with_flights else NO_FLIGHTS
    )


def _now() -> str:
    now = now_local()
    return (
        f"Nu is het {DAYS[now.weekday()]} {now.day} {MONTHS[now.month - 1]} {now.year} "
        f"({now:%Y-%m-%d}), {now:%H:%M} uur Nederlandse tijd."
    )

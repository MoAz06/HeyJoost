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

from .common import now_local

log = logging.getLogger(__name__)
# The Anthropic client logs every request; the requests say nothing new, so keep them out.
logging.getLogger("httpx2").setLevel(logging.WARNING)

MODEL = "claude-haiku-5-5"
# Dollars per million tokens for Haiku 5.5, to keep track of the daily budget.
PRICE_INPUT, PRICE_OUTPUT, PRICE_CACHE_WRITE, PRICE_CACHE_READ = 0.10, 0.50, 0.125, 0.01
# Booked before each call and settled after it, so a visitor who leaves halfway still counts.
ROUND_RESERVE = 0.002
MAX_TOKENS = 1024
MAX_ROUNDS = 5
MAX_LOOKUPS = 6
# Transitous was told a few calls per question, so one question looks up at most two stops.
MAX_STOP_LOOKUPS = 2
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

# Shown while Joost looks something up: "Hey Joost kijkt bij de NS".
WHERE = {
    "trein_vertrek": "bij de NS",
    "trein_aankomst": "bij de NS",
    "trein_reis": "bij de NS",
    "ov_vertrek": "bij de vervoerders",
    "vlucht_info": "bij Schiphol",
    "water_actueel": "bij Rijkswaterstaat",
    "schoolvakanties": "bij de Rijksoverheid",
    "huis_info": "in de BAG",
}

SYSTEM = """Je bent Joost, de assistent op heyjoost.nl. Je bent een AI (Claude, van Anthropic). Vraagt iemand of je een mens bent, dan zeg je eerlijk dat je een AI bent.

Je helpt met live informatie over Nederland, via je tools: treinen van de NS (vertrek, aankomst en reisadvies met prijs en drukte), bus, tram, metro en veerboot, vluchten via Schiphol, waterstand en watertemperatuur, schoolvakanties, en feiten over adressen en gebouwen uit de BAG.

Zo antwoord je:
- Zoek actuele feiten altijd op met een tool. Verzin nooit tijden, sporen, perrons, vertragingen of prijzen.
- Roep een tool meteen aan, zonder eerst te zeggen dat je gaat zoeken.
- Hou het kort: een paar zinnen, of een kort lijstje met "- ". Geen tabellen of kopjes. Zet tijden, sporen en perrons vet met **.
- Sluit af met de bron in een paar woorden, zoals "Bron: NS". De bron staat in het veld 'bron' van elk resultaat.
- Lukt iets niet of vind je niks, zeg dat dan eerlijk en stel een betere vraag voor.
- Mis je iets wat echt nodig is, zoals de plaats van een halte, vraag dat dan kort.
- Antwoord in de taal van de vraag. In het Nederlands schrijf je informeel, met je en jij.
- Gaat het om iets belangrijks, zoals een trein of vlucht halen, zeg dan dat de officiële app de laatste stand heeft.

Grenzen:
- Je helpt alleen met deze onderwerpen. Vraagt iemand iets anders, zoals huiswerk, code, nieuws, het weer of een verhaal, zeg dan vriendelijk dat je hier alleen live info over Nederland geeft, en dat iemand Hey Joost ook in een eigen AI kan zetten, zoals Claude of ChatGPT. Hoe dat moet, staat op deze pagina.
- Zoek geen mensen op en vraag niet naar persoonsgegevens. Een adres gebruik je alleen voor feiten over het gebouw.
- Gaat het over een noodgeval, verwijs dan naar 112."""

DAYS = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")
MONTHS = (
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
)


class _Day:
    """Today's spending and questions per visitor. In memory, so a restart starts the count over."""

    def __init__(self) -> None:
        self.date: date | None = None
        self.spent = 0.0
        self.asked: dict[str, int] = {}

    def today(self) -> "_Day":
        current = now_local().date()
        if current != self.date:
            self.date, self.spent, self.asked = current, 0.0, {}
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


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and _day.today().spent < _budget()


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
        messages = _messages(json.loads(body))
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
    day.asked[visitor] = day.asked.get(visitor, 0) + 1
    _busy[visitor] = now
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
        async for event in _answer(tools, messages):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    finally:
        _busy.pop(visitor, None)


async def _answer(tools: MCPServer, messages: list[dict[str, Any]]) -> AsyncIterator[dict[str, str]]:
    day = _day.today()
    definitions = [
        {"name": tool.name, "description": tool.description or "", "input_schema": tool.input_schema}
        for tool in await tools.list_tools()
    ]
    # The fixed part comes first and is cached; the clock changes every minute.
    system = [
        {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _now()},
    ]
    lookups: dict[str, int] = {}
    cost = 0.0
    for round_ in range(MAX_ROUNDS):
        day.spent += ROUND_RESERVE
        try:
            async with _anthropic().messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=definitions,
                # The last round answers with what it found so far.
                tool_choice={"type": "none" if round_ == MAX_ROUNDS - 1 else "auto"},
                messages=messages,
            ) as stream:
                async for event in stream:
                    if event.type == "text":
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
        blocks = [_param(block) for block in message.content if block.type == "tool_use" or getattr(block, "text", "")]
        results = await asyncio.gather(*(_lookup(tools, use, lookups) for use in uses))
        messages = [*messages, {"role": "assistant", "content": blocks}, {"role": "user", "content": results}]
    if message.stop_reason == "max_tokens":
        yield {"type": "text", "text": " …"}
    log.info(
        "Vraag het Joost: %d rondes, %d opzoekingen, $%.4f, vandaag $%.3f van $%.2f",
        round_ + 1, sum(lookups.values()), cost, day.spent, _budget(),
    )
    yield {"type": "done"}


def _param(block: Any) -> dict[str, Any]:
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return {"type": "text", "text": block.text}


async def _lookup(tools: MCPServer, use: Any, lookups: dict[str, int]) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "tool_result", "tool_use_id": use.id}
    lookups[use.name] = lookups.get(use.name, 0) + 1
    if sum(lookups.values()) > MAX_LOOKUPS or lookups.get("ov_vertrek", 0) > MAX_STOP_LOOKUPS:
        return {**result, "content": "Genoeg opgezocht voor één vraag. Antwoord met wat je al weet.", "is_error": True}
    try:
        output = await tools.call_tool(use.name, use.input)
    except UnexpectedToolError as exc:
        log.warning("Vraag het Joost: %s faalde (%s)", use.name, type(exc.__cause__).__name__)
        return {**result, "content": "Dit lukte even niet bij de bron. Probeer het later nog eens.", "is_error": True}
    except ToolError as exc:
        return {**result, "content": str(exc), "is_error": True}
    except Exception as exc:
        log.warning("Vraag het Joost: %s faalde (%s)", use.name, type(exc).__name__)
        return {**result, "content": "Dit lukte even niet bij de bron. Probeer het later nog eens.", "is_error": True}
    data = getattr(output, "structured_content", None)
    if data is None:
        text = " ".join(getattr(part, "text", "") for part in getattr(output, "content", []))
    else:
        text = json.dumps(data, ensure_ascii=False)
    return {**result, "content": text or "Geen resultaat.", "is_error": bool(getattr(output, "is_error", False))}


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


def _now() -> str:
    now = now_local()
    return (
        f"Nu is het {DAYS[now.weekday()]} {now.day} {MONTHS[now.month - 1]} {now.year} "
        f"({now:%Y-%m-%d}), {now:%H:%M} uur Nederlandse tijd."
    )

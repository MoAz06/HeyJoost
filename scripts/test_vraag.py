"""Offline checks of the chat loop in vraag.py with a made-up Haiku: what the chat does when an answer comes back
empty, and the trips of trein_reis as cards for the page. No key and no network needed.

Run from the project folder: uv run python scripts/test_vraag.py
"""

import asyncio
import json
import sys
from types import SimpleNamespace as Obj
from typing import Any

from nederland_mcp import tellers, vraag

USAGE = Obj(input_tokens=100, output_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=0)
THINKING = Obj(type="thinking", thinking="Even nadenken.", signature="handtekening")
failed: list[str] = []


def check(name: str, ok: bool, got: Any = None) -> None:
    print(f"{'ok  ' if ok else 'FOUT'} {name}" + ("" if ok else f": {got}"))
    if not ok:
        failed.append(name)


def reply(stop: str, *content: Obj) -> Obj:
    return Obj(stop_reason=stop, content=list(content), usage=USAGE, model=vraag.MODEL)


def says(text: str) -> Obj:
    return reply("end_turn", THINKING, Obj(type="text", text=text))


def looks_up(name: str, **given: Any) -> Obj:
    return reply("tool_use", THINKING, Obj(type="tool_use", id=f"zoek_{name}", name=name, input=given))


# Haiku only thought and wrote nothing.
SILENT = reply("end_turn", THINKING)


class Round:
    """One round of the made-up Haiku: it streams the text of its reply and ends with the reply."""

    def __init__(self, message: Obj) -> None:
        self.message = message

    async def __aenter__(self) -> "Round":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def __aiter__(self):
        for block in self.message.content:
            if block.type == "text":
                yield Obj(type="text", text=block.text)
            elif block.type == "tool_use":
                yield Obj(type="content_block_start", content_block=block)

    async def get_final_message(self) -> Obj:
        return self.message


class Haiku:
    """Gives the replies it was handed, one per round, and keeps what each round was asked."""

    def __init__(self, *replies: Obj) -> None:
        self.replies = list(replies)
        self.asked: list[dict[str, Any]] = []
        self.messages = self

    def stream(self, **request: Any) -> Round:
        self.asked.append(request)
        return Round(self.replies.pop(0))


class Tools:
    """Only the weather, which the chat credits to KNMI and Open-Meteo."""

    async def list_tools(self) -> list[Obj]:
        return [Obj(name="weer", description="Het weer.", input_schema={"type": "object", "properties": {}})]

    async def call_tool(self, name: str, given: dict[str, Any]) -> Obj:
        return Obj(structured_content={"result": {"plaats": "Utrecht", "temperatuur": 12}}, content=[], is_error=False)


def leg(van: str, vertrek: str, naar: str, aankomst: str, late: int = 0, spoor: str = "5b") -> dict[str, Any]:
    """A train as trein_reis gives it: the departure as planned, the delay apart, the arrival as expected."""
    return {"trein": "NS Intercity", "richting": naar, "van": van, "vertrek": vertrek, "vertraging_min": late,
            "spoor": spoor, "naar": naar, "aankomst": aankomst, "tussenstops": 3, "geannuleerd": False}


def trip(vertrek: str, aankomst: str, *parts: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"vertrek": vertrek, "aankomst": aankomst, "reistijd_min": 120, "drukte": "rustig", "prijs_euro": 26.5,
            "status": "normaal", "delen": list(parts), "link": "https://www.ns.nl/rpx?ctx=reis", **extra}


# To be in Nijmegen at 9:00 with 15 minutes of walking: the tool searched for 08:40, as in ns.trein_reis.
TRIPS = {
    "van": "Leiden Centraal",
    "naar": "Nijmegen",
    "gezocht_op": "aankomst 2026-11-14 08:40",
    "dag": "zaterdag 14 november",
    "dienstregeling": "Dit is de dienstregeling voor zaterdag 14 november.",
    "reisadviezen": [
        trip("06:17", "08:17", leg("Leiden Centraal", "06:17", "Utrecht Centraal", "07:04"),
             leg("Utrecht Centraal", "07:20", "Nijmegen", "08:17", late=3, spoor="19"),
             beste=True, op_bestemming="08:32", meldingen=["NS PrijsTijd Deal € 9,27 korting"]),
        trip("04:40", "07:47", leg("Leiden Centraal", "04:40", "Nijmegen", "07:47")),
        trip("05:00", "07:50", leg("Leiden Centraal", "05:00", "Nijmegen", "07:50"), link="https://elders.example/reis"),
        trip("05:23", "07:53", leg("Leiden Centraal", "05:23", "Schiphol Airport ✈", "05:50"),
             leg("Schiphol Airport ✈", "05:52", "Nijmegen", "07:53"), toeslag_euro=2.7),
    ],
    "bron": "NS",
}


class Trains(Tools):
    """trein_reis with TRIPS, or failing when asked for a station that does not exist."""

    async def call_tool(self, name: str, given: dict[str, Any]) -> Obj:
        if given.get("van") == "Nergens":
            raise vraag.ToolError("Geen station gevonden voor 'Nergens'.")
        return Obj(structured_content=TRIPS, content=[], is_error=False)


async def chat(haiku: Haiku, question: str, english: bool = False, tools: Tools | None = None) -> list[dict[str, Any]]:
    vraag._client = haiku
    messages = vraag._messages({"messages": [{"role": "user", "text": question}]})
    return [event async for event in vraag._answer(tools or Tools(), messages, "test", english)]


def text(events: list[dict[str, Any]]) -> str:
    return "".join(event["text"] for event in events if event["type"] == "text")


def last(request: dict[str, Any]) -> Any:
    """The last part of what a round was asked: the question, a result or a line for Claude."""
    content = request["messages"][-1]["content"]
    return content if isinstance(content, str) else content[-1].get("text", content[-1].get("type"))


async def main() -> None:
    haiku = Haiku(says("In Utrecht is het 12 graden."))
    events = await chat(haiku, "Wat voor weer is het in Utrecht?")
    check("een gewoon antwoord: één ronde, geen duwtje",
          len(haiku.asked) == 1 and text(events) == "In Utrecht is het 12 graden." and events[-1]["type"] == "done",
          events)

    question = "Wat voor weer is het in Utrecht?"
    nudges, empties = tellers._counts["chat duwtje"], tellers._counts["chat leeg"]
    haiku = Haiku(SILENT, says("In Utrecht is het 12 graden."))
    events = await chat(haiku, question)
    check("leeg en dan gevraagd met een duwtje: het antwoord komt door",
          text(events) == "In Utrecht is het 12 graden." and events[-1]["type"] == "done", events)
    check("het duwtje komt na de vraag, en de eerste ronde zag alleen de vraag",
          len(haiku.asked) == 2 and last(haiku.asked[0]) == question and last(haiku.asked[1]) == vraag.NUDGE
          and haiku.asked[1]["messages"][-1]["content"][0] == {"type": "text", "text": question},
          [last(request) for request in haiku.asked])
    check("na het duwtje kan Joost niets meer opzoeken, zoals het duwtje zegt",
          [request["tool_choice"]["type"] for request in haiku.asked] == ["auto", "none"],
          [request["tool_choice"] for request in haiku.asked])
    check("geteld als duwtje, niet als leeg",
          tellers._counts["chat duwtje"] == nudges + 1 and tellers._counts["chat leeg"] == empties,
          dict(tellers._counts))

    haiku = Haiku(looks_up("weer", plaats="Utrecht"), SILENT, SILENT)
    events = await chat(haiku, question)
    check("twee keer leeg na een zoekactie: een melding met opnieuw proberen",
          events[-1] == {"type": "error", "text": vraag.EMPTY, "opnieuw": True}, events)
    check("en geen bron als hele antwoord", text(events) == "" and "done" not in [e["type"] for e in events], events)
    results = haiku.asked[2]["messages"][-1]["content"]
    check("het duwtje komt na de resultaten, en maar één keer opnieuw",
          len(haiku.asked) == 3 and results[0]["type"] == "tool_result" and last(haiku.asked[1]) == vraag.IN_DUTCH
          and last(haiku.asked[2]) == vraag.NUDGE, [last(request) for request in haiku.asked])
    check("geteld als leeg",
          tellers._counts["chat duwtje"] == nudges + 2 and tellers._counts["chat leeg"] == empties + 1,
          dict(tellers._counts))

    haiku = Haiku(*[looks_up("weer", plaats=f"Utrecht {n}") for n in range(4)], says("Het is 12 graden."))
    events = await chat(haiku, question)
    check("na vier rondes zoeken hoort Joost met de resultaten dat opzoeken niet meer kan, eerder niet",
          [last(request) for request in haiku.asked[1:]] == [vraag.IN_DUTCH] * 3 + [vraag.NO_MORE]
          and [request["tool_choice"]["type"] for request in haiku.asked] == ["auto"] * 4 + ["none"]
          and text(events).startswith("Het is 12 graden."),
          [(request["tool_choice"]["type"], last(request)) for request in haiku.asked])

    haiku = Haiku(SILENT, SILENT)
    events = await chat(haiku, "What's the weather in Utrecht today?", english=True)
    check("in het Engels: het duwtje en de melding ook",
          last(haiku.asked[1]) == vraag.NUDGE_EN and events[-1].get("text") == vraag.IN_ENGLISH[vraag.EMPTY], events)

    haiku = Haiku(looks_up("weer", plaats="Utrecht"), says("Het is 12 graden."))
    events = await chat(haiku, question)
    check("met een antwoord komt de bron er nog wel onder",
          text(events) == "Het is 12 graden.\n\nBron: KNMI via Open-Meteo.com", text(events))

    # The trips as cards above the answer.
    asked = {"van": "Leiden", "naar": "Nijmegen", "datum": "2026-11-14", "tijd": "09:00", "aankomst": True, "lopen_min": 15}
    question = "Ik moet morgen om 9 uur in Nijmegen zijn, welke trein vanuit Leiden?"
    haiku = Haiku(looks_up("trein_reis", **asked), says("Neem de **06:17**, met een ruime overstap in Utrecht."))
    events = await chat(haiku, question, tools=Trains())
    check("de kaartjes komen na het zoeken en vóór de tekst",
          [event["type"] for event in events] == ["tool", "kaart", "text", "done"], [e["type"] for e in events])
    told = [block.get("text") for block in haiku.asked[1]["messages"][-1]["content"] if block["type"] == "text"]
    check("Joost hoort bij de resultaten dat de kaartjes er al staan", told == [vraag.CARDS, vraag.IN_DUTCH], told)
    check("en er komt geen bron onder: die staat op de kaartjes",
          text(events) == "Neem de **06:17**, met een ruime overstap in Utrecht.", text(events))

    card = events[1]["kaart"]
    check("het kaartje zegt wat er gezocht is: aankomst vóór 9:00, met een kwartier lopen, op een latere dag",
          {key: card.get(key) for key in ("van", "naar", "soort", "datum", "tijd", "lopen", "later")}
          == {"van": "Leiden Centraal", "naar": "Nijmegen", "soort": "aankomst", "datum": "2026-11-14", "tijd": "09:00",
              "lopen": 15, "later": True}, card)
    check("de beste eerst, dan de twee die het dichtst bij 9:00 aankomen",
          [trip["vertrek"] for trip in card["reizen"]] == ["06:17", "05:00", "05:23"], card["reizen"])
    best = card["reizen"][0]
    check("de beste: om 08:32 op je plek, 28 minuten vóór 9:00",
          best.get("beste") is True and best.get("bestemming") == "08:32" and best.get("over") == 28, best)
    check("de overstap met de vertraging erin: 07:04 aan, 07:23 weg, 19 minuten, ruim",
          best["overstappen"] == [{"station": "Utrecht Centraal", "min": 19, "oordeel": "ruim"}]
          and best["delen"][1]["vertrek"] == "07:23" and best["delen"][1]["vertraging"] == 3
          and best["delen"][0]["trein"] == "Intercity", best)
    check("een link alleen naar de NS zelf",
          best.get("link") == "https://www.ns.nl/rpx?ctx=reis" and "link" not in card["reizen"][1], card["reizen"][1])
    other = card["reizen"][2]
    check("2 minuten op Schiphol is krap, zonder het vliegtuigje, en de toeslag staat erbij",
          other["overstappen"] == [{"station": "Schiphol Airport", "min": 2, "oordeel": "krap"}]
          and other.get("toeslag") == 2.7 and other.get("over") == 67, other)

    haiku = Haiku(looks_up("trein_reis", van="Nergens", naar="Nijmegen"), says("Dat station ken ik niet."))
    events = await chat(haiku, "Trein van Nergens naar Nijmegen?", tools=Trains())
    told = [block.get("text") for block in haiku.asked[1]["messages"][-1]["content"] if block["type"] == "text"]
    check("geen kaartje en geen uitleg over kaartjes als het zoeken mislukt",
          "kaart" not in [event["type"] for event in events] and vraag.CARDS not in told, events)

    leaving = {**TRIPS, "gezocht_op": "vertrek 2026-11-14 04:30", "reisadviezen": [
        trip("04:40", "07:47", leg("Leiden Centraal", "04:40", "Nijmegen", "07:47"), beste=True),
        *TRIPS["reisadviezen"][2:], TRIPS["reisadviezen"][0]]}
    card = vraag._card({"van": "Leiden", "naar": "Nijmegen"}, json.dumps(leaving))
    check("bij vertrek: de beste en dan de eerste twee daarna, zonder 'over'",
          [trip["vertrek"] for trip in card["reizen"]] == ["04:40", "05:00", "05:23"] and card["tijd"] == "04:30"
          and "over" not in card["reizen"][0], card)
    night = {**TRIPS, "gezocht_op": "laatste trein 2026-11-14", "reisadviezen": [
        trip("00:36", "01:13", leg("Amsterdam Centraal", "00:36", "Leiden Centraal", "01:13"), laatste=True),
        trip("00:19", "00:54", leg("Amsterdam Centraal", "00:06", "Leiden Centraal", "00:54", late=13)),
        trip("00:20", "01:00", leg("Amsterdam Centraal", "00:20", "Leiden Centraal", "01:00"))]}
    card = vraag._card({"laatste": True}, json.dumps(night))
    check("de laatste trein: de vertraagde trein vertrekt om 00:19, 13 minuten later dan gepland",
          card["soort"] == "laatste" and card["datum"] == "2026-11-14" and "tijd" not in card
          and card["reizen"][0].get("laatste") is True and card["reizen"][1]["delen"][0]["vertrek"] == "00:19"
          and card["reizen"][1]["delen"][0]["vertraging"] == 13, card)
    check("over middernacht rekenen", vraag._gap("23:50", "00:10") == 20 and vraag._gap("00:10", "23:50") == -20)
    check("geen kaartje van iets anders dan een reisadvies",
          vraag._card({}, json.dumps({"result": {"plaats": "Utrecht"}})) is None and vraag._card({}, "geen json") is None)

    # The counts of this test are not for the log.
    tellers._counts.clear()
    print(f"\n{len(failed)} fout" if failed else "\nalles goed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

"""Offline checks of the chat loop in vraag.py with a made-up Haiku: what the chat does when an answer comes back
empty. No key and no network needed.

Run from the project folder: uv run python scripts/test_vraag.py
"""

import asyncio
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


async def chat(haiku: Haiku, question: str, english: bool = False) -> list[dict[str, Any]]:
    vraag._client = haiku
    messages = vraag._messages({"messages": [{"role": "user", "text": question}]})
    return [event async for event in vraag._answer(Tools(), messages, "test", english)]


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

    haiku = Haiku(SILENT, SILENT)
    events = await chat(haiku, "What's the weather in Utrecht today?", english=True)
    check("in het Engels: het duwtje en de melding ook",
          last(haiku.asked[1]) == vraag.NUDGE_EN and events[-1].get("text") == vraag.IN_ENGLISH[vraag.EMPTY], events)

    haiku = Haiku(looks_up("weer", plaats="Utrecht"), says("Het is 12 graden."))
    events = await chat(haiku, question)
    check("met een antwoord komt de bron er nog wel onder",
          text(events) == "Het is 12 graden.\n\nBron: KNMI via Open-Meteo.com", text(events))

    # The counts of this test are not for the log.
    tellers._counts.clear()
    print(f"\n{len(failed)} fout" if failed else "\nalles goed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

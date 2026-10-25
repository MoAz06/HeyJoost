"""Offline checks of how trein_reis chooses and describes trips, on made-up NS trips: no key and no network needed.

Run from the project folder: uv run python scripts/test_treinreis.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from nederland_mcp import common
from nederland_mcp.bronnen import ns
from nederland_mcp.common import AMSTERDAM

MONDAY = datetime(2026, 10, 26, tzinfo=AMSTERDAM)
FRIDAY_NOON = datetime(2026, 10, 23, 12, 0, tzinfo=AMSTERDAM)
failed: list[str] = []
asked: list[dict] = []


def check(name: str, ok: bool, got: Any = None) -> None:
    print(f"{'ok  ' if ok else 'FOUT'} {name}" + ("" if ok else f": {got}"))
    if not ok:
        failed.append(name)


def at(clock: str, seconds: int = 0) -> datetime:
    """Monday at this time; from 24:00 on it is the night after, as in a timetable."""
    hour, minute = map(int, clock.split(":"))
    return MONDAY + timedelta(hours=hour, minutes=minute, seconds=seconds)


def leg(van: str, naar: str, vertrek: str, aankomst: str, late: int = 0) -> dict:
    leaves = at(vertrek)
    return {
        "travelType": "PUBLIC_TRANSIT",
        "origin": {
            "name": van,
            "plannedDateTime": leaves.isoformat(),
            "actualDateTime": (leaves + timedelta(minutes=late)).isoformat(),
            "plannedTrack": "5",
        },
        "destination": {"name": naar, "plannedDateTime": at(aankomst).isoformat()},
        "product": {"displayName": "NS Intercity"},
        "direction": naar,
        # Three stops on the way, and one it passes without stopping.
        "stops": [{"name": van}, {"name": "a"}, {"name": "b"}, {"name": "c"}, {"name": "d", "passing": True}, {"name": naar}],
    }


def trip(*legs: dict, price: int = 1000, supplement: int = 0, status: str = "NORMAL") -> dict:
    trains = [part for part in legs if part["travelType"] == "PUBLIC_TRANSIT"]
    fare = {"priceInCents": price, **({"supplementInCents": supplement} if supplement else {})}
    return {"legs": list(legs), "transfers": len(trains) - 1, "status": status, "productFare": fare}


def direct(vertrek: str, aankomst: str, late: int = 0, **kwargs: Any) -> dict:
    return trip(leg("Leiden Centraal", "Den Haag Centraal", vertrek, aankomst, late), **kwargs)


def via(vertrek: str, overstap: str, verder: str, aankomst: str) -> dict:
    return trip(
        leg("Leiden Centraal", "Utrecht Centraal", vertrek, overstap),
        leg("Utrecht Centraal", "Den Haag Centraal", verder, aankomst),
    )


def pick(trips: list[dict], aankomst: bool = False, now: datetime | None = None) -> list[str]:
    """The trips _pick keeps, best first, as departure-arrival."""
    chosen = ns._pick(ns._options(trips), aankomst, now)
    return [f"{ns.fmt_time(departure)}-{ns.fmt_time(arrival)}" for departure, arrival, _, _ in chosen]


def at_now(now: datetime) -> None:
    ns.now_local = common.now_local = lambda: now


async def reis(
    trips: list[dict], now: datetime, earlier: tuple[list[dict], ...] = (), later: tuple[list[dict], ...] = (),
    **kwargs: Any,
) -> dict:
    """trein_reis from Leiden to Den Haag when NS has these trips, at this moment. Going back, NS gives the pages in
    `earlier`, each before the one before it, and going on the ones in `later`. What NS was asked ends up in `asked`."""
    asked.clear()

    async def get_json(url: str, params: dict, **kwargs: Any) -> dict:
        asked.append(params)
        way, _, number = params.get("context", "").partition(" ")
        number = int(number or 0)
        links = {}
        if way in ("", "terug") and number < len(earlier):
            links["scrollRequestBackwardContext"] = f"terug {number + 1}"
        if way in ("", "verder") and number < len(later):
            links["scrollRequestForwardContext"] = f"verder {number + 1}"
        page = (earlier if way == "terug" else later)[number - 1] if way else trips
        return {"trips": page, **links}

    async def station(name: str) -> dict:
        return {"code": name[:3].upper(), "namen": {"lang": name}}

    async def uncached(key: tuple, ttl: float, fetch: Any) -> Any:
        return await fetch()

    ns.get_json, ns._station, ns.cached = get_json, station, uncached
    at_now(now)
    return await ns.trein_reis("Leiden Centraal", "Den Haag Centraal", **kwargs)


def times(result: dict) -> list[str]:
    return [option["vertrek"] for option in result["reisadviezen"]]


async def main() -> None:
    # The key is only checked for being there; NS itself is not asked.
    os.environ.setdefault("NS_API_KEY", "offline")

    # Which trip is best.
    check("een overstap telt als 5 minuten: 3 minuten eerder met een overstap is niet beter",
          pick([direct("10:00", "11:00"), via("10:00", "10:20", "10:26", "10:57")]) == ["10:00-11:00"])
    check("10 minuten eerder met een overstap is wel beter",
          pick([direct("10:00", "11:00"), via("10:00", "10:20", "10:26", "10:50")]) == ["10:00-10:50", "10:00-11:00"])
    check("een krappe overstap telt dubbel",
          pick([direct("10:00", "11:00"), via("10:00", "10:20", "10:23", "10:52")]) == ["10:00-11:00"])
    check("met genoeg tijd om over te stappen wint dezelfde reis wel",
          pick([direct("10:00", "11:00"), via("10:00", "10:20", "10:26", "10:52")]) == ["10:00-10:52", "10:00-11:00"])
    check("een reis die eerder weg is en later aankomt valt weg",
          pick([direct("10:00", "10:40"), direct("10:05", "10:35")]) == ["10:05-10:35"])
    check("een minuut later weg en een kwartier later aan wint niet",
          pick([direct("10:00", "10:30"), direct("10:01", "10:45")]) == ["10:00-10:30", "10:01-10:45"])
    check("bij aankomst vertrekt de beste zo laat mogelijk",
          pick([direct("09:00", "09:30"), direct("09:15", "09:45"), direct("09:20", "09:50")], aankomst=True)
          == ["09:20-09:50", "09:00-09:30", "09:15-09:45"])

    # A train that leaves right now.
    now = at("10:00", 20)
    trips = [direct("10:00", "10:12"), direct("10:06", "10:18"), direct("10:15", "10:27")]
    check("zonder klok is de trein van nu de beste", pick(trips)[0] == "10:00-10:12")
    check("met klok is de trein van nu niet de beste, maar staat hij er wel bij",
          pick(trips, now=now) == ["10:06-10:18", "10:00-10:12", "10:15-10:27"], pick(trips, now=now))
    check("is er niets anders, dan is de trein van nu toch de beste",
          pick([direct("10:01", "10:13")], now=now) == ["10:01-10:13"])
    arriving = [direct("10:00", "10:40"), via("10:04", "10:14", "10:20", "10:42")]
    check("bij aankomst drukt de trein van nu een reis die je haalt niet weg",
          pick(arriving, aankomst=True) == ["10:00-10:40"]
          and pick(arriving, aankomst=True, now=now) == ["10:04-10:42", "10:00-10:40"], pick(arriving, True, now))
    later = at("10:00", 30)
    check("nu vertrekken is tot 2 minuten vooruit en een minuut terug",
          [ns._leaving(at(clock), later) for clock in ("09:59", "10:00", "10:02", "10:03")] == [False, True, True, False])

    result = await reis([direct("09:58", "10:12", late=2), direct("10:06", "10:18"), direct("10:15", "10:27")], now)
    first, second = result["reisadviezen"][:2]
    check("trein_reis: de beste is de eerste die je haalt", first["vertrek"] == "10:06" and first.get("beste")
          and "vertrekt_nu" not in first, first)
    check("trein_reis: de trein van nu staat erbij met vertrekt_nu, en met de tijd volgens het spoorboekje",
          second["vertrek"] == "10:00" and second.get("vertrekt_nu") and "beste" not in second
          and second["delen"][0]["vertrek"] == "09:58" and second["delen"][0]["vertraging_min"] == 2, second)
    result = await reis([direct("09:40", "10:10"), direct("10:00", "10:30"), direct("10:10", "10:38")], now,
                        tijd="10:40", aankomst=True)
    check("trein_reis bij aankomst: een trein die al weg is valt weg, die van nu staat erbij",
          times(result) == ["10:10", "10:00"] and result["reisadviezen"][1].get("vertrekt_nu"), result["reisadviezen"])
    result = await reis([direct("10:00", "10:12"), direct("10:06", "10:18")], at("12:00"), tijd="10:00")
    check("trein_reis voor een tijd die al voorbij is: zoals altijd, zonder vertrekt_nu",
          times(result) == ["10:00", "10:06"] and not any("vertrekt_nu" in o for o in result["reisadviezen"]))
    result = await reis([direct("09:30", "09:50"), direct("09:45", "10:05")], now, tijd="10:20", aankomst=True)
    check("trein_reis: zijn alle treinen al weg, dan staan ze er toch", times(result) == ["09:45", "09:30"])
    result = await reis([direct("20:50", "21:30"), direct("20:53", "21:35"), direct("20:54", "21:40")], at("20:59", 10),
                        later=([direct("21:04", "21:44"), direct("21:10", "21:50")],))
    check("trein_reis: na een rij treinen die net weg zijn, komt de volgende pagina van NS erbij",
          times(result) == ["21:04", "21:10"] and len(asked) == 2, result["reisadviezen"])

    # What trein_reis says about a trip.
    result = await reis([direct("08:00", "08:35")], FRIDAY_NOON, datum="2026-10-26", tijd="09:00", aankomst=True,
                        lopen_min=15)
    check("lopen_min: het laatste stuk en de marge gaan eraf",
          result["gezocht_op"] == "aankomst 2026-10-26 08:40"
          and result["op_tijd_voor"] == "09:00, met 15 minuten lopen en 5 minuten marge", result)
    check("lopen_min: hoe laat je er bent", result["reisadviezen"][0].get("op_bestemming") == "08:50",
          result["reisadviezen"][0])
    check("dag in woorden", result["dag"] == "maandag 26 oktober (over 3 dagen)", result["dag"])
    result = await reis([direct("08:00", "08:35")], FRIDAY_NOON, datum="2026-10-26", tijd="09:00", aankomst=True,
                        lopen_min=0)
    check("lopen_min 0: alleen de marge", result["gezocht_op"] == "aankomst 2026-10-26 08:55"
          and result["op_tijd_voor"] == "09:00, met 5 minuten marge"
          and "op_bestemming" not in result["reisadviezen"][0], result)
    result = await reis([via("08:00", "08:20", "08:22", "08:50")], FRIDAY_NOON, datum="2026-10-26", tijd="07:55")
    option = result["reisadviezen"][0]
    check("overstaptijd met oordeel", option["overstaptijd"] == ["Utrecht Centraal: 2 minuten, krap"], option)
    check("tussenstops zonder de stations waar hij doorrijdt", option["delen"][0]["tussenstops"] == 3, option["delen"])
    walking = [leg("Leiden Centraal", "Utrecht Centraal", "10:00", "10:20"),
               {"travelType": "WALK", "plannedDurationInMinutes": 3},
               leg("Utrecht Centraal", "Den Haag Centraal", "10:28", "10:50")]
    check("lopen tussen twee treinen gaat van de overstaptijd af", ns._changes(walking) == [("Utrecht Centraal", 5)])
    walk = {"travelType": "WALK", "plannedDurationInMinutes": 20,
            "origin": {"name": "Rotterdam Zuid", "plannedDateTime": at("19:02").isoformat()},
            "destination": {"name": "Rotterdam Stadion", "plannedDateTime": at("19:22").isoformat()}}
    check("een stuk lopen is een deel met lopen_min, niet een trein zonder naam", ns._leg(walk) == {
        "lopen_min": 20, "van": "Rotterdam Zuid", "vertrek": "19:02", "naar": "Rotterdam Stadion", "aankomst": "19:22",
    }, ns._leg(walk))
    lines = [ns._change_line("Leiden Centraal", gap) for gap in (1, 5, 10, -1)]
    check("oordeel per overstap", lines == [
        "Leiden Centraal: 1 minuut, krap",
        "Leiden Centraal: 5 minuten, genoeg",
        "Leiden Centraal: 10 minuten, ruim",
        "Leiden Centraal: haal je niet, de trein vertrekt voor je aankomt",
    ], lines)
    at_now(FRIDAY_NOON)
    check("vandaag en morgen", [ns._day(FRIDAY_NOON), ns._day(FRIDAY_NOON + timedelta(days=1))]
          == ["vrijdag 23 oktober (vandaag)", "zaterdag 24 oktober (morgen)"])
    result = await reis([direct("08:00", "08:30", supplement=320), direct("08:10", "08:38", status="CANCELLED")],
                        FRIDAY_NOON, datum="2026-10-26", tijd="07:55")
    check("toeslag en een reis die niet gaat", result["reisadviezen"][0].get("toeslag_euro") == 3.2
          and result.get("niet_mogelijk") == ["vertrek 08:10, aankomst 08:38"], result)
    many = [direct(f"08:{minute:02d}", f"08:{minute + 30:02d}") for minute in range(0, 29, 4)]
    result = await reis(many, FRIDAY_NOON, datum="2026-10-26", tijd="07:55")
    check("hooguit 5 reizen", len(result["reisadviezen"]) == ns.MAX_TRIPS, times(result))

    # The last train.
    morning = [direct("29:51", "30:20"), direct("30:21", "30:50")]
    result = await reis(morning, FRIDAY_NOON, (
        [direct("23:21", "23:50"), direct("23:51", "24:20"), direct("24:21", "24:50")],
        [direct("22:21", "22:50"), direct("22:51", "23:20")],
    ), datum="2026-10-26", laatste=True)
    check("laatste: terugzoeken vanaf 4 uur 's nachts, tot er 3 reizen van de avond zijn",
          [params["dateTime"] for params in asked] == ["2026-10-27T04:00:00+01:00"] * 2, asked)
    check("laatste: de laatste bovenaan, dan de andere op tijd, zonder de ochtend",
          times(result) == ["00:21", "23:21", "23:51"] and result["reisadviezen"][0].get("laatste")
          and "nachttreinen" not in result, result["reisadviezen"])
    check("laatste: gezocht_op en dag", result["gezocht_op"] == "laatste trein 2026-10-26"
          and result["dag"] == "maandag 26 oktober (over 3 dagen)", result)
    result = await reis([direct("28:02", "28:30"), *morning], FRIDAY_NOON, (
        [direct("25:04", "25:30"), direct("26:02", "26:30"), direct("27:02", "27:30")],
        [direct("23:54", "24:40"), direct("24:11", "24:45"), via("24:20", "24:40", "24:46", "25:10")],
    ), datum="2026-10-26", laatste=True)
    check("laatste met nachttreinen: de laatste van de avond bovenaan, de nacht in een zin, 4 uur hoort bij de ochtend",
          times(result) == ["00:20", "23:54", "00:11"] and result.get("nachttreinen") == "Daarna gaan er 's nachts "
          "nog treinen: om 01:04 (aankomst 01:30), om 02:02 (aankomst 02:30) en om 03:02 (aankomst 03:30).", result)
    trips = [direct("23:41", "24:45"), via("23:41", "24:00", "24:10", "24:50"), direct("23:30", "24:30"),
             direct("23:30", "24:20", price=1320, supplement=320)]
    chosen = [f"{ns.fmt_time(o[0])}-{ns.fmt_time(o[1])}" for o in ns._latest(ns._options(trips))]
    check("laatste: een reis die in alles slechter is valt weg, een duurdere die eerder aankomt niet",
          chosen == ["23:41-00:45", "23:30-00:20", "23:30-00:30"], chosen)
    result = await reis(morning, at("24:30"), ([direct("23:51", "24:20"), direct("24:40", "25:10")],), laatste=True)
    check("laatste na middernacht zonder datum: nog dezelfde nacht, en wat al weg is valt weg",
          times(result) == ["00:40"] and result["dag"] == "maandag 26 oktober (vannacht)", result)
    try:
        await reis(morning, at("24:30"), ([direct("23:51", "24:20"), direct("24:21", "24:50")],), laatste=True)
        check("laatste als alles al weg is: zeg het, met de eerste van de ochtend", False, "geen fout")
    except ToolError as error:
        check("laatste als alles al weg is: zeg het, met de eerste van de ochtend",
              "geen trein meer" in str(error) and "05:51" in str(error), error)

    print(f"\n{len(failed)} fout" if failed else "\nalles goed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

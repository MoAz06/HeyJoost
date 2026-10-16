"""Vraag het Joost: the chat on the website, which answers with the Hey Joost tools through Claude.

The page sends the conversation so far and the server keeps nothing. A daily budget and a limit per
visitor keep a busy day from eating the API credits.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
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
from .bronnen import reisplanner, schiphol
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
# And at most two trips from door to door, such as there and back.
MAX_PLAN_LOOKUPS = 2
# Flights only exist in this chat, with the limits agreed with Schiphol (see bronnen/schiphol.py).
MAX_FLIGHT_LOOKUPS = 2
MAX_QUESTION = 500
MAX_REPLY = 4000
MAX_MESSAGES = 12
# The lookups of earlier answers come along too.
MAX_BODY = 48_000
# What one earlier lookup may ask, as JSON: a place and a few options fit easily.
MAX_INPUT = 400
MAX_BUSY = 8
BUSY_TIMEOUT = 120
# Right after a stop the old answer can take a moment to close, so a new question waits that long for it.
STOP_WAIT = 2.0
STREAM_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}

OFF = "Vraag het Joost staat nu uit. Zet Joost in je eigen AI, dan kan het altijd."
# Every limit says when it ends: the counts start over at midnight, Dutch time.
MIDNIGHT = "Vanaf middernacht kan het weer. Of zet Joost in je eigen AI, dan kan het altijd."
TIRED = f"Joost is even moe, de vragen voor vandaag zijn op. {MIDNIGHT}"
CROWDED = "Het is even druk bij Joost. Probeer het zo nog eens."
STILL_BUSY = "Joost is nog bezig met je vorige vraag."
TOO_LONG = f"Je vraag is te lang. Hou hem onder de {MAX_QUESTION} tekens."
CONFUSED = "Dat snapte Joost niet. Ververs de pagina en probeer het nog eens."
UNREACHABLE = "Joost is even niet bereikbaar. Probeer het zo nog eens."
REFUSED = "Daar kan Joost niet mee helpen."
# The same messages for the English chat, which a browser without Dutch gets.
MIDNIGHT_EN = "You can ask again from midnight, Dutch time. Or add Joost to your own AI to use it any time."
IN_ENGLISH = {
    OFF: "Ask Joost is off right now. Add Joost to your own AI to use it any time.",
    TIRED: f"Joost is a bit tired, today’s questions are used up. {MIDNIGHT_EN}",
    CROWDED: "It’s busy at Joost right now. Try again in a moment.",
    STILL_BUSY: "Joost is still working on your previous question.",
    TOO_LONG: f"Your question is too long. Keep it under {MAX_QUESTION} characters.",
    CONFUSED: "Joost didn’t understand that. Refresh the page and try again.",
    UNREACHABLE: "Joost can’t be reached right now. Try again in a moment.",
    REFUSED: "Joost can’t help with that.",
}
# For Claude: in the English chat a question without a clear language, like just a place or a flight number, gets an
# English answer.
ENGLISH_PAGE = (
    " De bezoeker gebruikt de chat in het Engels. Is de taal van de vraag niet duidelijk, antwoord dan in het Engels."
)
# For Claude, not the visitor: without "not looked up" it reports a skipped lookup as a failed one.
ENOUGH = (
    "Niet opgezocht: dit is het maximum voor één vraag, het is dus niet mislukt. Antwoord met wat je al weet, "
    "zeg eerlijk wat je niet hebt opgezocht en dat de gebruiker dat apart kan vragen."
)
FLIGHTS_DONE = (
    "Niet opgezocht: deze bezoeker heeft vandaag al het maximum aan vluchten opgezocht, het is dus niet mislukt. "
    "Zeg dat vluchten vanaf middernacht weer kunnen, en verwijs voor nu naar schiphol.nl of de app van de "
    "luchtvaartmaatschappij."
)
FLIGHTS_FULL = (
    "Niet opgezocht: het maximum aan vluchten voor vandaag is bereikt, voor de hele site, het is dus niet mislukt. "
    "Zeg dat vluchten vanaf middernacht weer kunnen, en verwijs voor nu naar schiphol.nl of de app van de "
    "luchtvaartmaatschappij."
)
FAILED = "Dit lukte even niet bij de bron. Probeer het later nog eens."
# For Claude: the page keeps what Joost looked up for an earlier answer, but not what came back. "Not kept" alone
# read as "found nothing", and Joost then confessed to making up the answer he had in fact looked up.
EARLIER = (
    "Gelukt: deze zoekactie gaf resultaten, en je antwoord hierna komt daaruit. Ze staan hier niet meer bij, om het "
    "gesprek kort te houden."
)
# After long results, as with the travel advice for Egypt, Haiku often answered a Dutch question in English. A note
# right after the results keeps it Dutch. Whether a question is Dutch, the small words tell.
IN_DUTCH = "Schrijf het antwoord in het Nederlands."
DUTCH_WORDS = frozenset(
    "ik je jij jullie u het een de van naar wat hoe wanneer waar welke welk kan kun heb hebt moet mijn niet geen er "
    "om te voor bij op met en ook nog dit deze die dat zijn wordt gaat morgen vandaag nu gisteren hoeveel waarom".split()
)
ENGLISH_WORDS = frozenset(
    "the and what how when where which can could do does i my for with of to are will there it you need from this "
    "that today tomorrow now yesterday much many why".split()
)

# Shown while Joost looks something up: "Hey Joost kijkt bij de NS".
WHERE = {
    "trein_vertrek": "bij de NS",
    "trein_aankomst": "bij de NS",
    "trein_reis": "bij de NS",
    "ov_vertrek": "bij de vervoerders",
    "water_actueel": "bij Rijkswaterstaat",
    "water_verwachting": "bij Rijkswaterstaat",
    "trein_storingen": "bij de NS",
    "schoolvakanties": "bij de Rijksoverheid",
    "huis_info": "in de BAG",
    "vlucht_info": "bij Schiphol",
    "weer": "bij Open-Meteo (KNMI)",
    "regen": "op de regenradar van het KNMI",
    "weerwaarschuwingen": "bij het KNMI",
    "stroomprijs": "bij de stroombeurs",
    "buurt_info": "bij het CBS",
    "reisadvies": "bij Buitenlandse Zaken",
    "regelzaken": "bij de overheid",
    "bekendmakingen": "in de officiële bekendmakingen",
    "verkeer": "bij NDW",
    "open_in_de_buurt": "in OpenStreetMap",
    "reis_plannen": "bij Transitous",
}
# In the English chat: "Hey Joost is checking NS".
WHERE_EN = {
    "trein_vertrek": "NS",
    "trein_aankomst": "NS",
    "trein_reis": "NS",
    "ov_vertrek": "the transport companies",
    "water_actueel": "Rijkswaterstaat",
    "water_verwachting": "Rijkswaterstaat",
    "trein_storingen": "NS",
    "schoolvakanties": "the Dutch government",
    "huis_info": "the address register (BAG)",
    "vlucht_info": "Schiphol",
    "weer": "Open-Meteo (KNMI)",
    "regen": "the KNMI rain radar",
    "weerwaarschuwingen": "the KNMI",
    "stroomprijs": "the power exchange",
    "buurt_info": "CBS",
    "reisadvies": "Foreign Affairs",
    "regelzaken": "the government",
    "bekendmakingen": "the official announcements",
    "verkeer": "NDW",
    "open_in_de_buurt": "OpenStreetMap",
    "reis_plannen": "Transitous",
}
# Named in every answer that uses their data: Schiphol as promised, Open-Meteo, KNMI, SMARD and CBS for their
# licence (CC BY 4.0), and OpenStreetMap for the ODbL. The word to look for in the answer, and what to add when
# Claude left it out.
CREDITS = {
    "open_in_de_buurt": ("openstreetmap", "© OpenStreetMap-bijdragers"),
    "vlucht_info": ("schiphol", "Schiphol"),
    "weer": ("open-meteo", "KNMI via Open-Meteo.com"),
    "regen": ("knmi", "KNMI"),
    "weerwaarschuwingen": ("knmi", "KNMI"),
    "stroomprijs": ("smard", "Bundesnetzagentur | SMARD.de"),
    "buurt_info": ("cbs", "CBS"),
}
# Where a question came from, as the page reports it. Only counted, never stored with anything else. A "chip" is an
# example in the chat that asks right away, "aangevuld" one that put a question in the field for you to finish, and
# "vervolg" a follow-up button under an answer.
ORIGINS = ("getypt", "voorbeeld", "chip", "aangevuld", "vervolg", "vast", "link", "opnieuw")
# Errors where the same question may well work a moment later, so the page offers to try again.
PASSING = (STILL_BUSY, CROWDED, UNREACHABLE)

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
# Not an MCP tool either: the chat counts the questions Joost cannot answer, by a topic from this list and never the
# question itself, to show what people miss. The Tellers line in the log has them as "chat mist <onderwerp>".
MISSED = {
    "verkeer": "flitsers en reistijd met de auto",
    "parkeren": "parkeren en parkeergarages",
    "brandstof": "benzineprijzen, tankstations en laadpalen",
    "afval": "afvalkalender en containers",
    "lucht": "luchtkwaliteit, pollen en hooikoorts",
    "winkels": "andere winkels dan supermarkten en apotheken, markten en koopzondagen",
    "zorg": "huisartsenpost, dienstapotheek en ziekenhuis",
    "overheid": "verkiezingen en zaken van de overheid die je niet kunt opzoeken",
    "geld": "belasting, toeslagen of uitkeringen uitrekenen, en prijzen",
    "uitgaan": "evenementen, musea, bioscoop en restaurants",
    "nieuws": "nieuws, politiek en sport",
    "pakket": "pakketpunten en pakketautomaten, zoals van DPD, PostNL of DHL",
    "buitenland": "reizen, weer of vervoer buiten Nederland",
    "vluchten": "vluchten die je niet kunt opzoeken",
    "anders": "iets anders over Nederland",
    "geen_nederland": "geen vraag over Nederland, zoals huiswerk, code of een verhaal",
}
MISSED_TOOL = {
    "name": "gemiste_vraag",
    "description": (
        "Telt een vraag die je met je andere tools niet kunt beantwoorden, alleen het onderwerp; de vraag zelf wordt "
        "nooit bewaard. Roep dit aan nadat je je antwoord hebt geschreven, één keer per onderwerp."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "onderwerp": {
                "type": "string",
                "enum": list(MISSED),
                "description": "; ".join(f"{name}: {what}" for name, what in MISSED.items()),
            },
        },
        "required": ["onderwerp"],
    },
}

SYSTEM = """Je bent Joost, de assistent op heyjoost.nl. Je bent een AI (Claude, van Anthropic). Vraagt iemand of je een mens bent, dan zeg je eerlijk dat je een AI bent.

Je helpt met live informatie over Nederland, via je tools: treinen van de NS (vertrek, aankomst en reisadvies met prijs en drukte, en storingen en werkzaamheden op het spoor), bus, tram, metro en veerboot, {vluchten}het weer (temperatuur, regen en wind, tot een week vooruit), de regenradar voor de komende twee uur, de weerwaarschuwingen van het KNMI, waterstand en watertemperatuur, eb en vloed, de stroomprijs per kwartier, schoolvakanties, feiten over adressen en gebouwen uit de BAG, cijfers over buurten van het CBS, het reisadvies van Buitenlandse Zaken voor het buitenland, officiële antwoorden over regelzaken met de overheid (zoals toeslagen, AOW, paspoort en rijbewijs) en wat je bij je gemeente regelt, bekendmakingen rond een adres (zoals vergunningen om te bouwen of bomen te kappen), het verkeer op de weg nu (files, ongelukken, afsluitingen en werkzaamheden), en supermarkten, apotheken en pinautomaten in de buurt met hun openingstijden.

Zo antwoord je:
- Antwoord altijd in de taal van de laatste vraag. Is die Nederlands, dan antwoord je in het Nederlands, ook als er Engelse woorden in een bron staan. In het Nederlands schrijf je informeel, met je en jij, ook als de bron u schrijft.
- Zoek actuele feiten altijd op met een tool. Verzin nooit tijden, sporen, perrons, vertragingen of prijzen.
- Roep een tool meteen aan, zonder eerst te zeggen dat je gaat zoeken.
- Hou het kort: een paar zinnen, of een kort lijstje met "- ". Geen tabellen of kopjes. Zet tijden, sporen en perrons vet met **.
- Schrijf eenvoudig, op B1-niveau, ook in andere talen: korte zinnen, gewone woorden, het belangrijkste eerst. Leg een moeilijk woord of een afkorting de eerste keer kort uit tussen haakjes, zoals kWh (kilowattuur, de eenheid waarin je stroom betaalt), NAP (het nulpunt voor hoogtes in Nederland, ongeveer het gemiddelde zeeniveau) of WOZ-waarde (de waarde van een huis volgens de gemeente). Namen van bronnen, zoals NS of KNMI, hoef je niet uit te leggen.
- Sluit af met de bron in een paar woorden, zoals "Bron: NS". Dat woord schrijf je in de taal van je antwoord, in het Engels dus "Source: NS". De bron staat in het veld 'bron' van elk resultaat.
- Lukt iets niet of vind je niks, zeg dat dan eerlijk en stel een betere vraag voor.
- Mis je iets wat echt nodig is, zoals de plaats van een halte, vraag dat dan kort.
{reisplanner}
- Reken bij een overstap uit hoeveel minuten je hebt en noem dat getal, zoals "dan heb je 12 minuten om over te stappen". Minder dan 5 minuten is krap, 5 tot 10 minuten is genoeg en vanaf 10 minuten is het ruim.
- Gaat het om een later moment, geef dan datum en tijd mee aan de tool.
- Staat er in een resultaat een 'let_op', zoals een staking, noem dat dan altijd, vóór de tijden. Gaat het bericht over een andere dag dan de vraag, zeg dat er dan kort bij. Staan er bij een treinreis 'meldingen', zoals een bus in plaats van de trein, noem die dan ook.
- Vraagt iemand of de treinen rijden, of naar storingen of werkzaamheden, gebruik dan trein_storingen, met het station en de dagen als die genoemd worden. Noem wat er rijdt (bussen of minder treinen), waar, tot wanneer, en het advies van NS.
- Bij eb en vloed noem je de tijden van hoog- en laagwater en de hoogte in cm boven of onder NAP, en het meetpunt als dat verderop ligt. Staat er 'volgens': 'verwachting', gebruik dan die tijden en hoogtes; de getijtabel noem je alleen als iemand daarom vraagt of als de wind het water flink hoger of lager zet.
- Bij de stroomprijs noem je de prijs met belasting in cent per kWh, met de uitleg van kWh erbij, en zeg je dat de opslag van de leverancier en de vaste kosten er nog bij komen. De kale prijs noem je als iemand daarom vraagt of als die onder nul is. Vraagt iemand wanneer stroom goedkoop is, noem dan de goedkoopste momenten vanaf nu. Noem als bron "Bundesnetzagentur | SMARD.de".
- Bij cijfers over een buurt noem je het gebied, de cijfers die bij de vraag passen met hun jaar, en zo nodig de gemeente of Nederland om te vergelijken. Het zijn gemiddelden voor het hele gebied. Oordeel niet over een buurt of de mensen die er wonen: zeg niet of een buurt goed, slecht, veilig of onveilig is. Noem als bron "CBS".
- Bij reisadvies noem je de kleurcode en voor welk gebied die geldt, wat er speelt en de datum van de laatste wijziging. Bij oranje of rood zeg je dat het advies snel kan veranderen en dat nederlandwereldwijd.nl de laatste stand heeft. Noem als bron "Buitenlandse Zaken".
- Bij regelzaken vertel je het officiële antwoord kort in je eigen woorden, met de link naar de pagina. Zeg erbij dat het voor iemands eigen situatie anders kan zijn en dat de officiële pagina de laatste stand heeft. Gaat het om iets wat je bij de gemeente regelt, zoals een paspoort, parkeervergunning of grofvuil, en weet je de gemeente niet, vraag dat dan kort. Noem als bron "Rijksoverheid" of de gemeente.
- Bij bekendmakingen noem je per stuk wat er speelt, de straat, of het een aanvraag of een besluit is, en de datum, met de link. Wil iemand reageren of bezwaar maken, vertel dan wat in 'uitleg' staat en dat de bekendmaking zelf zegt hoe en tot wanneer. Het is geen juridisch advies. Noem als bron "officiële bekendmakingen".
- Bij het verkeer noem je per bericht wat er is, waar (weg, afrit en richting) en sinds of tot wanneer, de files eerst. Zeg erbij dat het ongecontroleerde berichten zijn en dat de radio of je navigatie de laatste stand heeft. Vind je niks, zeg dan dat NDW daar nu niks meldt, niet dat het er rustig is. Noem als bron "NDW".
- Bij open in de buurt zeg je eerst vanaf waar je zocht, zoals in 'rond' staat. Noem dan per plek de naam, het adres of de straat, hoe ver ongeveer, en of het nu open is en tot hoe laat, of wanneer het weer opengaat. Staat er een 'tip', noem die dan kort. Zeg erbij dat de openingstijden kunnen afwijken, zeker op feestdagen. Vraagt iemand naar een apotheek als die dicht is, noem dan wat er over de dienstapotheek staat. Noem als bron "OpenStreetMap".
- Zeg nooit dat iets niet lukte als je het niet hebt geprobeerd.
- Je eerdere antwoorden in dit gesprek heb je echt opgezocht. Van je eerdere zoekacties zie je nog met welke tool en waarmee je zocht; de resultaten staan er niet meer bij, om het gesprek kort te houden, maar je antwoorden komen eruit. Twijfel er dus niet aan en zoek ze niet opnieuw op, behalve als de gebruiker daarom vraagt of zegt dat iets niet klopt.
- Gaat het om iets belangrijks, zoals een trein halen, zeg dan dat de officiële app de laatste stand heeft.

Grenzen:
- Je helpt alleen met deze onderwerpen. Vraagt iemand iets anders, zoals huiswerk, code, nieuws of een verhaal, zeg dan vriendelijk dat je hier alleen live info over Nederland geeft, en dat iemand Hey Joost ook in een eigen AI kan zetten, zoals Claude of ChatGPT. Hoe dat moet, staat op deze pagina.
- Kun je een vraag of een deel ervan niet beantwoorden omdat je er geen tool voor hebt, schrijf dan eerst je antwoord en roep daarna gemiste_vraag aan met het onderwerp. Niet als een tool iets niet vond of faalde, en niet bij een groet of een bedankje. Verwijs dan gerust naar een bekende bron, zoals de ANWB of de gemeente, maar verzin geen webadressen.
{vluchtgrenzen}
- Gaat het om regen nu of de komende twee uur, zoals droog naar huis fietsen, gebruik dan regen. Voor later gebruik je weer.
- Het weer is een verwachting uit weermodellen. Noem bij het weer als bron precies "KNMI via Open-Meteo.com", en bij de regenradar en de waarschuwingen "KNMI". Ook hier in de taal van je antwoord: "Bron: KNMI via Open-Meteo.com", in het Engels "Source: KNMI via Open-Meteo.com".
- Bij een weerwaarschuwing noem je de code, wat, waar en wanneer, en het advies van het KNMI als dat erbij staat. Bij code oranje of rood zeg je ook dat knmi.nl de laatste stand heeft.
- Zoek geen mensen op en vraag niet naar persoonsgegevens. Een adres gebruik je alleen voor feiten over het gebouw, of voor de haltes, winkels of bekendmakingen in de buurt, nooit om uit te zoeken wie er woont of wie iets heeft aangevraagd. Voor haltes, winkels en bekendmakingen is een straat en plaats genoeg, dus vraag niet om een huisnummer of postcode.
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
# Door to door: in pieces, or with the journey planner of Transitous once they agree to it (HEYJOOST_REISPLANNER,
# see bronnen/reisplanner.py). Filled in by _system().
NO_PLANNER = (
    "- Een routeplanner heb je niet, dus van deur tot deur doe je in stukjes. Moet iemand vanaf een adres met de "
    "trein naar een andere plaats, zoek dan tegelijk de trein met trein_reis vanaf het grote station in de buurt, "
    "en met ov_vertrek de bus of tram vanaf het adres of de straat met naar dat station, dus niet naar de "
    "eindbestemming. Noem de halte waar je opstapt en hoe ver die ongeveer is. De afstand is hemelsbreed, lopen "
    "is iets verder.\n"
    "- Moet iemand op een vaste tijd ergens zijn, plan dan terug vanaf die tijd en zoek de stukjes na elkaar in "
    "plaats van tegelijk. Zoek eerst de trein met trein_reis, met aankomst=true. Als tijd neem je de afspraak min "
    "het laatste stuk (lopen: reken 10 tot 15 minuten als je het niet weet) en 5 minuten marge. Bijvoorbeeld: "
    "afspraak om 10:00 en een kwartier lopen, dan zoek je met 09:40. Neem de bovenste reis, want die komt het "
    "laatst op tijd aan. Een eerdere trein noem je hooguit als extra zekerheid. Zoek daarna de bus of tram naar "
    "het station, met als tijd een half uur voor die trein, en kies de laatste die minstens 5 minuten voor "
    "vertrek bij het station is."
)
PLANNER = (
    "- Van deur tot deur met het ov gebruik je reis_plannen, met het adres, de straat, het station of de plaats "
    "van vertrek en van bestemming. Noem per reis de vertrek- en aankomsttijd en de overstappen, en per deel de "
    'lijn, waar je instapt en uitstapt en hoe lang je loopt. Noem als bron "Transitous".\n'
    "- Moet iemand op een vaste tijd ergens zijn, gebruik dan reis_plannen met die tijd en aankomst=true, en neem "
    "de bovenste reis: die vertrekt het laatst en is nog op tijd.\n"
    "- Gaat het alleen om de trein, of om de prijs of de drukte, gebruik dan trein_reis."
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


def per_day() -> int:
    return int(os.environ.get("VRAAG_PER_DAG") or 30)


def _flights_per_day() -> int:
    return int(os.environ.get("VLUCHT_PER_DAG") or 10)


def _flights_per_site() -> int:
    return int(os.environ.get("VLUCHT_SITE_PER_DAG") or 300)


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and _day.today().spent < _budget()


def state() -> str:
    """For the home page: "aan", "vol" when today's budget is used up, or "uit" without a key."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "uit"
    return "aan" if available() else "vol"


def flights() -> bool:
    """Whether the chat can look up flights now, so the home page only mentions them then."""
    return available() and schiphol.available() and _day.today().all_flights < _flights_per_site()


async def ask(request: Request, tools: MCPServer) -> Response:
    english = _english(request)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _refuse(503, OFF, english)
    origin = request.headers.get("origin")
    if origin and origin.partition("://")[2] != request.headers.get("host"):
        return _refuse(403, CONFUSED, english)

    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            return _refuse(413, TOO_LONG, english)
    # An earlier lookup only goes back to Claude with a tool he has now.
    known = {tool.name for tool in await tools.list_tools()} | ({FLIGHT_TOOL["name"]} if schiphol.available() else set())
    try:
        data = json.loads(body)
        messages = _messages(data, known)
    except ValueError as exc:
        return _refuse(400, str(exc) if str(exc) in (TOO_LONG, CONFUSED) else CONFUSED, english)

    day = _day.today()
    if day.spent >= _budget():
        return _refuse(429, TIRED, english)
    visitor = _visitor(request)
    for _ in range(round(STOP_WAIT / 0.1)):
        started = _busy.get(visitor)
        if started is None or time.monotonic() - started > BUSY_TIMEOUT:
            break
        await asyncio.sleep(0.1)
    now = time.monotonic()
    for key, started in list(_busy.items()):
        if now - started > BUSY_TIMEOUT:
            del _busy[key]
    if visitor in _busy:
        return _refuse(429, STILL_BUSY, english)
    if len(_busy) >= MAX_BUSY:
        return _refuse(503, CROWDED, english)
    if day.asked.get(visitor, 0) >= per_day():
        if english:
            return _refuse(429, f"You’ve already asked {per_day()} questions today, the maximum. {MIDNIGHT_EN}")
        return _refuse(429, f"Je hebt vandaag al {per_day()} vragen gesteld, het maximum. {MIDNIGHT}")
    if visitor not in day.asked:
        tellers.tel("chat bezoeker")
    day.asked[visitor] = day.asked.get(visitor, 0) + 1
    _busy[visitor] = now
    tellers.tel("chat")
    if data.get("via") in ORIGINS:
        tellers.tel(f"chat via {data['via']}")
    if data.get("app") is True:
        tellers.tel("chat in app")
    return StreamingResponse(
        _events(tools, messages, visitor, english), media_type="text/event-stream", headers=STREAM_HEADERS
    )


def _refuse(status: int, text: str, english: bool = False) -> JSONResponse:
    return JSONResponse({"error": _say(text, english), "opnieuw": text in PASSING}, status_code=status)


def _say(text: str, english: bool) -> str:
    return IN_ENGLISH.get(text, text) if english else text


def _english(request: Request) -> bool:
    """Whether the visitor gets English messages: the English chat asks with "en", and a browser without Dutch too."""
    tags = [part.split(";")[0].strip().lower() for part in request.headers.get("accept-language", "").split(",")]
    tags = [tag for tag in tags if tag and tag != "*"]
    return bool(tags) and not any(tag == "nl" or tag.startswith("nl-") for tag in tags)


def _visitor(request: Request) -> str:
    # Behind Cloudflare the visitor's address is in cf-connecting-ip. It can be faked by skipping
    # Cloudflare, which only buys more questions per day: the daily budget still holds.
    address = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not address and request.client:
        address = request.client.host
    return hashlib.sha256(_salt + address.encode()).hexdigest()[:16]


def _messages(data: Any, known: set[str] | frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """The conversation from the page as API messages, taking turns and ending with the question. An earlier answer
    comes with the lookups Joost did for it, as tool calls without their results. Without them he once read his own
    answer, missed that he had looked it up, and told the visitor he had not."""
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
        # The conversation starts with a question.
        if not text or (not messages and role != "user"):
            continue
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n\n" + text
            continue
        uses = _uses(item.get("tools"), known, len(messages)) if role == "assistant" else []
        if uses:
            results = [{"type": "tool_result", "tool_use_id": use["id"], "content": EARLIER} for use in uses]
            messages += [{"role": "assistant", "content": uses}, {"role": "user", "content": results}]
        messages.append({"role": role, "content": text})
    if not messages or messages[-1]["role"] != "user":
        raise ValueError(CONFUSED)
    return messages


def _uses(tools: Any, known: set[str] | frozenset[str], at: int) -> list[dict[str, Any]]:
    """Earlier lookups from the page as tool calls: only tools Joost has, and only with a short input."""
    uses = []
    for number, use in enumerate(tools[:MAX_LOOKUPS] if isinstance(tools, list) else []):
        name = use.get("tool") if isinstance(use, dict) else None
        given = use.get("input") if isinstance(use, dict) else None
        if isinstance(name, str) and name in known and isinstance(given, dict):
            if len(json.dumps(given, ensure_ascii=False)) <= MAX_INPUT:
                uses.append({"type": "tool_use", "id": f"eerder_{at}_{number}", "name": name, "input": given})
    return uses


async def _events(
    tools: MCPServer, messages: list[dict[str, Any]], visitor: str, english: bool = False
) -> AsyncIterator[str]:
    try:
        async for event in _answer(tools, messages, visitor, english):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    finally:
        _busy.pop(visitor, None)


async def _answer(
    tools: MCPServer, messages: list[dict[str, Any]], visitor: str, english: bool = False
) -> AsyncIterator[dict[str, Any]]:
    day = _day.today()
    definitions = [
        {"name": tool.name, "description": tool.description or "", "input_schema": tool.input_schema}
        for tool in await tools.list_tools()
    ]
    with_flights = schiphol.available()
    if with_flights:
        definitions.append(FLIGHT_TOOL)
    definitions.append(MISSED_TOOL)
    # The fixed part comes first and is cached; the clock changes every minute.
    system = [
        {"type": "text", "text": _system(with_flights), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _now() + (ENGLISH_PAGE if english else "")},
    ]
    question = messages[-1]["content"]
    note = [{"type": "text", "text": IN_DUTCH}] if _dutch(question) else []
    lookups: dict[str, int] = {}
    said, used, missed = "", set(), set()
    looked: list[dict[str, Any]] = []
    cost = 0.0
    for round_ in range(MAX_ROUNDS):
        day.spent += ROUND_RESERVE
        before = len(said)
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
                    elif (
                        event.type == "content_block_start"
                        and event.content_block.type == "tool_use"
                        and event.content_block.name != MISSED_TOOL["name"]
                    ):
                        name = event.content_block.name
                        where = WHERE_EN.get(name, "the source") if english else WHERE.get(name, "bij de bron")
                        yield {"type": "tool", "where": where, "tool": name}
                message = await stream.get_final_message()
        except anthropic.APIError as exc:
            day.spent -= ROUND_RESERVE
            text = _api_error(exc, day)
            yield {"type": "error", "text": _say(text, english), "opnieuw": text in PASSING}
            return
        spent = _cost(message.usage)
        day.spent += spent - ROUND_RESERVE
        cost += spent
        if message.stop_reason == "refusal":
            yield {"type": "error", "text": _say(REFUSED, english), "opnieuw": False}
            return
        uses = [block for block in message.content if block.type == "tool_use"]
        if message.stop_reason != "tool_use" or not uses:
            break
        missed.update(_missed(use.input) for use in uses if use.name == MISSED_TOOL["name"])
        # Counted after the answer, as asked: then the answer is complete and needs no other round.
        if all(use.name == MISSED_TOOL["name"] for use in uses) and said[before:].strip():
            break
        blocks = [_param(block) for block in message.content if block.type != "text" or block.text]
        results = await asyncio.gather(*(_lookup(tools, use, lookups, visitor) for use in uses))
        used.update(use.name for use, result in zip(uses, results) if not result["is_error"])
        # The page sends these back with the next question, so Joost knows what he looked up for this answer.
        looked += [
            {"tool": use.name, "input": use.input}
            for use, result in zip(uses, results) if not result["is_error"] and use.name != MISSED_TOOL["name"]
        ]
        messages = [*messages, {"role": "assistant", "content": blocks}, {"role": "user", "content": [*results, *note]}]
    if message.stop_reason == "max_tokens":
        yield {"type": "text", "text": " …"}
    missing = [credit for name, (word, credit) in CREDITS.items() if name in used and word not in said.lower()]
    if missing:
        # Even if Claude forgets, the answer names these sources.
        # In the language of the answer: English for an English question, and in the English chat for any question
        # that is not Dutch.
        nl, en = _counts(question)
        label = "Source" if en > nl or (english and nl <= en) else "Bron"
        yield {"type": "text", "text": f"\n\n{label}: " + ", ".join(missing)}
    for topic in sorted(missed):
        tellers.tel(f"chat mist {topic}")
    log.info(
        "Vraag het Joost: %d rondes, %d opzoekingen, $%.4f, vandaag $%.3f van $%.2f",
        round_ + 1, sum(lookups.values()), cost, day.spent, _budget(),
    )
    # How many questions are left today, so the page can warn before the last one.
    yield {"type": "done", "over": max(0, per_day() - day.asked.get(visitor, 0)), "gezocht": looked}


def _dutch(question: str) -> bool:
    nl, en = _counts(question)
    return nl > en


def _counts(question: str) -> tuple[int, int]:
    """How many small Dutch and English words a question has."""
    words = re.findall(r"[a-z']+", question.lower())
    return sum(word in DUTCH_WORDS for word in words), sum(word in ENGLISH_WORDS for word in words)


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
    if use.name == MISSED_TOOL["name"]:
        return {**result, "content": "Geteld.", "is_error": False}
    # Each lookup runs in its own task, so this only marks the chat's own calls for the counts.
    tellers.via.set("chat")
    lookups[use.name] = lookups.get(use.name, 0) + 1
    if (
        sum(lookups.values()) > MAX_LOOKUPS
        or lookups.get("ov_vertrek", 0) > MAX_STOP_LOOKUPS
        or lookups.get("reis_plannen", 0) > MAX_PLAN_LOOKUPS
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


def _missed(question: Any) -> str:
    topic = question.get("onderwerp") if isinstance(question, dict) else None
    return topic if topic in MISSED else "anders"


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
    ).replace("{reisplanner}", PLANNER if reisplanner.AAN else NO_PLANNER)


def _now() -> str:
    now = now_local()
    return (
        f"Nu is het {DAYS[now.weekday()]} {now.day} {MONTHS[now.month - 1]} {now.year} "
        f"({now:%Y-%m-%d}), {now:%H:%M} uur Nederlandse tijd."
    )

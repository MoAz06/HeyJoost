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
from datetime import date, timedelta
from typing import Any

import anthropic
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from . import tellers
from .bronnen import ns, reisplanner, schiphol
from .common import now_local

log = logging.getLogger(__name__)
# The Anthropic client logs every request; the requests say nothing new, so keep them out.
logging.getLogger("httpx2").setLevel(logging.WARNING)

MODEL = "claude-haiku-5-5"
# Dollars per million tokens for Haiku 5.5, to keep track of the daily budget.
PRICE_INPUT, PRICE_OUTPUT, PRICE_CACHE_WRITE, PRICE_CACHE_READ = 0.10, 0.50, 0.125, 0.01
# Booked before each call and settled after it, so a visitor who leaves halfway still counts.
ROUND_RESERVE = 0.002
# Haiku thinks before it answers, and that counts as output too: 1024 cut off some answers halfway, and 2048 one in
# three answers for a train there and back with the night trains. 3072 still cut off the hardest question of the eval
# now and then, to the Erasmus MC and back as late as possible without the Intercity direct: its last round thinks
# close to 3000 tokens, and the answer stopped after a sentence.
MAX_TOKENS = 4096
# Low was not faster in tests, and high not better.
EFFORT = "medium"
MAX_ROUNDS = 5
MAX_LOOKUPS = 6
# Transitous was told a few calls per question, so one question looks up at most three stops. Its plans from door to
# door are counted in reisplanner.PER_QUESTION.
MAX_STOP_LOOKUPS = 3
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
ELSEWHERE = "Vraag het Joost werkt op heyjoost.nl."
UNREACHABLE = "Joost is even niet bereikbaar. Probeer het zo nog eens."
REFUSED = "Daar kan Joost niet mee helpen."
# When the answer stays empty, even after the nudge (see NUDGE).
EMPTY = "Joost kwam er even niet uit. Probeer het nog eens, of stel je vraag wat anders."
# The same messages for the English chat, which a browser without Dutch gets.
MIDNIGHT_EN = "You can ask again from midnight, Dutch time. Or add Joost to your own AI to use it any time."
IN_ENGLISH = {
    OFF: "Ask Joost is off right now. Add Joost to your own AI to use it any time.",
    TIRED: f"Joost is a bit tired, today’s questions are used up. {MIDNIGHT_EN}",
    CROWDED: "It’s busy at Joost right now. Try again in a moment.",
    STILL_BUSY: "Joost is still working on your previous question.",
    TOO_LONG: f"Your question is too long. Keep it under {MAX_QUESTION} characters.",
    CONFUSED: "Joost didn’t understand that. Refresh the page and try again.",
    ELSEWHERE: "Ask Joost works on heyjoost.nl.",
    UNREACHABLE: "Joost can’t be reached right now. Try again in a moment.",
    REFUSED: "Joost can’t help with that.",
    EMPTY: "Joost got stuck for a moment. Try again, or ask your question a little differently.",
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
# For Claude, with the results before the round that can no longer look anything up. All six empty answers seen came
# in that round, after four rounds of lookups: Haiku still wanted to look up the bus and did not know it could not.
# Such a round asked again stayed empty 36 times out of 36, and with this line 0 times out of 24. A round that had
# everything answered as well with it as without.
NO_MORE = (
    "Opzoeken kan nu niet meer. Schrijf dus nu je antwoord met wat je hebt gevonden. Kon je iets niet opzoeken, zeg dat "
    "dan eerlijk."
)
NO_MORE_EN = (
    "You can't look anything up anymore. So write your answer now with what you found. If you couldn't look something "
    "up, say so honestly."
)
# And with the second try, when an answer still came back empty. Told only to write its answer, such a round stayed
# empty 7 times out of 12; told that looking up is over too, 0 times out of 48.
NUDGE = f"Je vorige antwoord bleef leeg: je dacht na, maar schreef niets voor de bezoeker. {NO_MORE}"
NUDGE_EN = f"Your previous answer came back empty: you thought, but wrote nothing for the visitor. {NO_MORE_EN}"
# For Claude: the page keeps what Joost looked up for an earlier answer, but not what came back. "Not kept" alone
# read as "found nothing", and Joost then confessed to making up the answer he had in fact looked up.
EARLIER = (
    "Gelukt: deze zoekactie gaf resultaten, en je antwoord hierna komt daaruit. Ze staan hier niet meer bij, om het "
    "gesprek kort te houden."
)
# After long results, as with the travel advice for Egypt, Haiku often answered a Dutch question in English, and after
# the Dutch departure board an English question in Dutch. A note right after the results keeps the language of the
# question. Which language that is, the small words tell.
IN_DUTCH = "Schrijf het antwoord in het Nederlands."
IN_ENGLISH_NOTE = "Write the answer in English."
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
# The trips of trein_reis also go to the page, as cards above the answer (see _card): the best and two others.
CARD_TRIPS = 3
# What else than a train a part of a planned trip can be (see reisplanner.MODES).
VEHICLES = {"bus", "tram", "metro", "veerboot"}
# For Claude, with the results of trein_reis: the cards say the trips, so the answer only has to choose.
CARDS = (
    "De bezoeker ziet deze reizen al als kaartjes boven je antwoord: per reis de tijden, de reistijd, de overstappen "
    "met het oordeel, de sporen, de drukte, de prijs en een link naar de NS. Herhaal die dus niet en reken niets uit. "
    "Schrijf hooguit twee korte zinnen, samen niet meer dan 30 woorden, zonder lijstje: welke reis je aanraadt, met "
    "de vertrektijd vet, en in een paar woorden waarom. Heeft een andere reis iets wat de beste mist, zoals geen "
    "overstap of geen toeslag, noem die dan met de tijd en alleen dat verschil. Noem daarnaast alleen wat niet op de "
    "kaartjes staat: 'let_op', een storing of werkzaamheden uit 'meldingen', 'niet_mogelijk', 'nachttreinen', een "
    "trein met 'vertrekt_nu', of een korte vraag over het laatste stuk. Zeg niets over storingen of vertraging die "
    "niet in het resultaat staan. Geen 'Bron: NS' en geen zin over de NS-app."
)
CARDS_EN = (
    "The visitor already sees these trips as cards above your answer: per trip the times, the travel time, the "
    "transfers with how much time they leave, the platforms, how busy it is, the price and a link to NS. So don't "
    "repeat those and don't work anything out. Write at most two short sentences, 30 words at most together, without "
    "a list: which trip you recommend, with the departure time in bold, and in a few words why. If another trip has "
    "something the best one lacks, like no transfer or no supplement, name it by its time and only that difference. "
    "Besides that only mention what is not on the cards: 'let_op', a disruption or works from 'meldingen', "
    "'niet_mogelijk', 'nachttreinen', a train with 'vertrekt_nu', or a short question about the last stretch. Say "
    "nothing about disruptions or delays that are not in the results. No 'Source: NS' and no sentence about the NS "
    "app."
)
NS_LINK = "https://www.ns.nl/"
SEARCHED = re.compile(r"(aankomst|vertrek) (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})|laatste trein (\d{4}-\d{2}-\d{2})")
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
- Sluit af met de bron in een paar woorden, zoals "Bron: NS". Dat woord schrijf je in de taal van je antwoord, in het Engels dus "Source: NS". De bron staat in het veld 'bron' van elk resultaat. Bij trein_reis hoeft het niet, want de kaartjes boven je antwoord komen van de NS.
- Lukt iets niet of vind je niks, zeg dat dan eerlijk en stel een betere vraag voor.
- Mis je iets wat echt nodig is, zoals de plaats van een halte, vraag dat dan kort.
{reisplanner}
- Noem bij een overstap hoeveel minuten je hebt en of dat krap, genoeg of ruim is, zoals "in Utrecht heb je 12 minuten om over te stappen, dat is ruim". Bij trein_reis staat dat al op de kaartjes (zie hieronder); noem je het toch, neem dan het oordeel uit 'overstaptijd' letterlijk over (5 minuten is dus genoeg, niet krap) en reken het niet zelf uit. Anders reken je zelf: minder dan 5 minuten is krap, 5 tot 10 minuten is genoeg en vanaf 10 minuten is het ruim.
- Gaat het om een trein van de ene plaats naar de andere, ook bij "de volgende trein", gebruik dan trein_reis en niet trein_vertrek. Noemt iemand bij zo'n reis een tijd, zoals "vrijdag om 17:10 van Delft naar Gouda", dan is dat de vertrektijd. aankomst=true is alleen voor als iemand op een tijd ergens moet zijn. Wil iemand geen toeslag betalen, of niet met de Intercity direct of de ICE, geef dan zonder_toeslag=true mee, ook bij de reis terug. De eerste trein van een dag zoek je vanaf 04:00, want treinen tussen middernacht en 4 uur horen bij de nacht ervoor. Wil iemand de laatste trein, of zo laat mogelijk terug, gebruik dan laatste=true met alleen de datum: dan zoekt de tool zelf tot in de nacht. Raad de reis met 'laatste' aan, of de laatste die past bij wat iemand wil, zoals zonder toeslag. Vertrekt die na middernacht, zeg dan dat het 's nachts is. Staat er 'nachttreinen', neem die zin dan altijd over, want daarmee kan iemand nog later terug.
- In deze chat ziet de bezoeker de reizen van trein_reis als kaartjes boven je antwoord: per reis de tijden, de reistijd, de overstappen met het oordeel, de sporen, de drukte, de prijs met een eventuele toeslag, hoe laat iemand er is en, na een tik, de hele route. Dat gaat voor op de andere regels over wat je bij een treinreis noemt. Herhaal het dus niet, reken niets uit en maak geen lijstje. Schrijf één of twee korte zinnen, samen niet meer dan 30 woorden: welke reis je aanraadt, met de vertrektijd vet, en in een paar woorden waarom, zoals "zonder overstap" of "het snelst, met één overstap in Utrecht". Noem daarnaast alleen wat niet op de kaartjes staat: 'let_op', 'meldingen', 'niet_mogelijk', 'nachttreinen', een trein met 'vertrekt_nu', een krappe overstap, of een korte vraag over het laatste stuk. Zeg niet "de bovenste" of "het eerste kaartje", maar noem de tijd. Vraagt iemand iets wat op de kaartjes staat, zoals het spoor, geef dan gewoon antwoord. "Bron: NS" en een zin over de NS-app horen er niet onder, want de kaartjes komen van de NS en linken ernaar.
- Bij trein_reis staat de beste reis bovenaan, met 'beste'. Die komt zo vroeg mogelijk aan met zo min mogelijk overstappen, of bij aankomst=true vertrekt die zo laat mogelijk en is nog op tijd. Raad die reis aan, ook als een andere korter onderweg is. Wil iemand iets, zoals geen toeslag of niet overstappen, raad dan de eerste reis aan die daarbij past. Noem daarna hooguit één andere reis, en alleen als die iets heeft wat de beste mist: geen overstap of geen toeslag. Noem die met de tijd en alleen dat verschil. Hoe vaak een trein onderweg stopt, staat in 'tussenstops'; zeg alleen dat hij nergens stopt als dat 0 is. Hoeft iemand niet over te stappen, zeg dan "zonder overstap". Welke dag de reis is, staat in 'dag', en de tijden gelden voor die dag: noem die dag, en zeg geen "vandaag" als het een andere dag is. Vertrekt de beste trein binnen een paar minuten, zeg dat dan en noem ook de volgende. Een reis met 'vertrekt_nu' vertrekt nu of binnen 2 minuten en is daarom niet de beste, want die haal je alleen als je al bij de trein staat. Noem die er kort bij, zoals "de trein van 17:15 vertrekt nu, die haal je alleen als je al op het perron staat"; die telt niet als die ene andere reis. Heeft de beste zelf 'vertrekt_nu', zeg dan dat die nu vertrekt.
- Raad je een treinreis met 'toeslag_euro' aan, zeg dan kort dat er een toeslag in de prijs zit voor de Intercity direct of de ICE; hoeveel staat op het kaartje. Staan er reizen in 'niet_mogelijk', zeg dan kort dat die niet gaan.
- Gaat het om een later moment, geef dan datum en tijd mee aan de tool. "Volgende week dinsdag" is de dinsdag in volgende week, en welke dagen dat zijn, staat bij de tijd van nu.
- Staat er in een resultaat een 'let_op', zoals een staking, noem dat dan altijd, vóór de tijden. Gaat het bericht over een andere dag dan de vraag, zeg dat er dan kort bij. Staan er bij een treinreis 'meldingen', zoals een bus in plaats van de trein, noem die dan ook.
- Vraagt iemand of de treinen rijden, of naar storingen of werkzaamheden, gebruik dan trein_storingen, met het station en de dagen als die genoemd worden. Noem wat er rijdt (bussen of minder treinen), waar, tot wanneer, en het advies van NS.
- Bij eb en vloed noem je de tijden van hoog- en laagwater en de hoogte in cm boven of onder NAP, en het meetpunt als dat verderop ligt. Staat er 'volgens': 'verwachting', gebruik dan die tijden en hoogtes; de getijtabel noem je alleen als iemand daarom vraagt of als de wind het water flink hoger of lager zet.
- Bij de stroomprijs noem je de prijs met belasting in cent per kWh, met de uitleg van kWh erbij, en zeg je dat de opslag van de leverancier en de vaste kosten er nog bij komen. De kale prijs noem je als iemand daarom vraagt of als die onder nul is. Vraagt iemand wanneer stroom goedkoop is, noem dan de goedkoopste momenten vanaf nu. Noem als bron "Bundesnetzagentur | SMARD.de".
- Bij cijfers over een buurt noem je het gebied, de cijfers die bij de vraag passen met hun jaar, en zo nodig de gemeente of Nederland om te vergelijken. Het zijn gemiddelden voor het hele gebied. Oordeel niet over een buurt of de mensen die er wonen: zeg niet of een buurt goed, slecht, veilig of onveilig is. Noem als bron "CBS".
- Bij reisadvies noem je de kleurcode en voor welk gebied die geldt, wat er speelt en de datum van de laatste wijziging. Bij oranje of rood zeg je dat het advies snel kan veranderen en dat nederlandwereldwijd.nl de laatste stand heeft. Noem als bron "Buitenlandse Zaken".
- Bij regelzaken vertel je het officiële antwoord kort in je eigen woorden, met de link naar de pagina. Zeg erbij dat het voor iemands eigen situatie anders kan zijn en dat de officiële pagina de laatste stand heeft. Gaat het om iets wat je bij de gemeente regelt, zoals een paspoort, parkeervergunning of grofvuil, en weet je de gemeente niet, vraag dat dan kort. Noem als bron "Rijksoverheid" of de gemeente.
- Bij bekendmakingen noem je per stuk wat er speelt, de straat, of het een aanvraag of een besluit is, en de datum, met de link. Wil iemand reageren of bezwaar maken, vertel dan wat in 'uitleg' staat en dat de bekendmaking zelf zegt hoe en tot wanneer. Het is geen juridisch advies. Noem als bron "officiële bekendmakingen".
- Bij het verkeer noem je per bericht wat er is, waar (weg, afrit en richting) en sinds of tot wanneer, de files eerst. Vraagt iemand alleen naar files, of welke wegen dicht zijn, geef dan soort 'files' of 'afsluitingen' mee. Bij afsluitingen noem je eerst de wegen die helemaal dicht zijn; afritten en parkeerplaatsen vat je kort samen, en met 'per_weg' noem je waar nog meer iets dicht is. Zeg erbij dat het ongecontroleerde berichten zijn en dat de radio of je navigatie de laatste stand heeft. Vind je niks, zeg dan dat NDW daar nu niks meldt, niet dat het er rustig is. Noem als bron "NDW".
- Bij open in de buurt zeg je eerst vanaf waar je zocht, zoals in 'rond' staat. Noem dan per plek de naam, het adres of de straat, hoe ver ongeveer, en of het nu open is en tot hoe laat, of wanneer het weer opengaat. Staat er een 'tip', noem die dan kort. Zeg erbij dat de openingstijden kunnen afwijken, zeker op feestdagen. Vraagt iemand naar een apotheek als die dicht is, noem dan wat er over de dienstapotheek staat. Noem als bron "OpenStreetMap".
- Zeg nooit dat iets niet lukte als je het niet hebt geprobeerd.
- Je eerdere antwoorden in dit gesprek heb je echt opgezocht. Van je eerdere zoekacties zie je nog met welke tool en waarmee je zocht; de resultaten staan er niet meer bij, om het gesprek kort te houden, maar je antwoorden komen eruit. Twijfel er dus niet aan en zoek ze niet opnieuw op, behalve als de gebruiker daarom vraagt of zegt dat iets niet klopt.
- Gaat het om iets belangrijks, zoals een trein halen, zeg dan dat de officiële app de laatste stand heeft. Bij trein_reis hoeft dat niet, want de kaartjes linken naar de NS.

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
    "- Een routeplanner heb je niet, maar trein_reis neemt als van en naar ook een plaats, adres of plek, zoals "
    "'Radboudumc Nijmegen' of 'Stadhuisplein 1 Leiden'. Geef de woorden van de bezoeker door, en de tijd waarop hij "
    "weg wil of er moet zijn: de tool kiest het station en rekent zelf met het lopen of de bus en 5 minuten marge, "
    "dus trek daar zelf niets voor af.\n"
    "- Staat er bij een stuk 'niet_berekend', doe dan wat daar staat, en noem waar je opstapt, waar je uitstapt en "
    "hoe laat je er bent ('op_bestemming')."
)
PLANNER = (
    "- Voor een reis met het ov gebruik je trein_reis, ook van deur tot deur: van en naar mogen een station, plaats, "
    "adres of plek zijn, zoals 'Radboudumc Nijmegen' of 'Stadhuisplein 1 Leiden'. Geef de woorden van de bezoeker "
    "door, en de tijd waarop hij weg wil of er moet zijn: de tool kiest zelf de trein of plant de reis met bus, tram "
    "of metro, en rekent het lopen en 5 minuten marge zelf uit, dus trek daar zelf niets voor af.\n"
    "- Staat er 'reisplanner' bij, dan komt de reis van de reisplanner van Transitous: noem dan bij bus, tram of "
    "metro de lijn en waar je instapt en uitstapt. Staat er bij een stuk 'niet_berekend', doe dan wat daar staat."
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
    # Railway's own address skips Cloudflare, so there a visitor could write his own address (see _visitor).
    if request.headers.get("host", "").endswith(".up.railway.app"):
        return _refuse(403, ELSEWHERE, english)

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
    # Behind Cloudflare the visitor's address is in cf-connecting-ip, otherwise Railway's edge puts it in x-real-ip.
    # Who reaches Railway without Cloudflare can write his own cf-connecting-ip. On Railway's own address the chat
    # refuses (see ask), so that takes finding Railway's servers, and it only buys more questions per day: the
    # daily budget still holds. The first address in x-forwarded-for is never used, since anyone can write it.
    address = request.headers.get("cf-connecting-ip") or request.headers.get("x-real-ip") or ""
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
    # The plans of the journey planner are counted per question (see reisplanner.PER_QUESTION).
    reisplanner.new_question()
    # trein_reis takes the journey planner itself where it must, so the chat has no reis_plannen to choose wrongly.
    definitions = [
        {"name": tool.name, "description": tool.description or "", "input_schema": tool.input_schema}
        for tool in await tools.list_tools() if tool.name != "reis_plannen"
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
    nl, en = _counts(question)
    note = [{"type": "text", "text": IN_DUTCH if nl > en else IN_ENGLISH_NOTE}] if nl != en else []
    lookups: dict[str, int] = {}
    said, used, missed = "", set(), set()
    looked: list[dict[str, Any]] = []
    cost = 0.0
    nudged = False
    # One round more than MAX_ROUNDS, only for an answer that came back empty.
    for round_ in range(MAX_ROUNDS + 1):
        day.spent += ROUND_RESERVE
        before = len(said)
        try:
            async with _anthropic().messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                output_config={"effort": EFFORT},
                system=system,
                tools=definitions,
                # The last round answers with what it found so far, and so does the round after an empty answer.
                tool_choice={"type": "none" if round_ >= MAX_ROUNDS - 1 or nudged else "auto"},
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
            # Now and then Haiku only thinks and says nothing (see NUDGE). Asked once more and told why, it answers.
            if message.stop_reason == "end_turn" and not said[before:].strip() and not nudged:
                nudged = True
                tellers.tel("chat duwtje")
                messages = _nudged(messages, NUDGE_EN if _in_english(question, english) else NUDGE)
                continue
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
        # The trips go to the page as cards, which show them above the answer, straight from NS (see _card).
        cards = False
        for use, result in zip(uses, results):
            if use.name == "trein_reis" and not result["is_error"] and (card := _card(result["content"])):
                cards = True
                if card.get("bron") == "Transitous":
                    # The line above the cards said NS when the lookup began, and this trip came from the planner.
                    where = WHERE_EN["reis_plannen"] if english else WHERE["reis_plannen"]
                    yield {"type": "tool", "where": where, "tool": "reis_plannen"}
                yield {"type": "kaart", "kaart": card}
        # Haiku hears it right after the results too: from the system prompt alone it still wrote out every trip.
        told = [{"type": "text", "text": CARDS_EN if _in_english(question, english) else CARDS}] if cards else []
        content = [*results, *told, *note]
        if round_ + 1 >= MAX_ROUNDS - 1:
            # The next round can no longer look anything up, and Haiku hears it with these results (see NO_MORE).
            content.append({"type": "text", "text": NO_MORE_EN if _in_english(question, english) else NO_MORE})
        messages = [*messages, {"role": "assistant", "content": blocks}, {"role": "user", "content": content}]
    for topic in sorted(missed):
        tellers.tel(f"chat mist {topic}")
    if not said.strip():
        # Still nothing after the nudge. Without this the page only had its own line, with no button to try again,
        # and with a source to name the answer was just "Bron: KNMI".
        tellers.tel("chat leeg")
        log.info("Vraag het Joost: leeg na %d rondes, %d opzoekingen, $%.4f", round_ + 1, sum(lookups.values()), cost)
        yield {"type": "error", "text": _say(EMPTY, english), "opnieuw": True}
        return
    if message.stop_reason == "max_tokens":
        yield {"type": "text", "text": " …"}
    missing = [credit for name, (word, credit) in CREDITS.items() if name in used and word not in said.lower()]
    if missing:
        # Even if Claude forgets, the answer names these sources, in the language of the answer.
        label = "Source" if _in_english(question, english) else "Bron"
        yield {"type": "text", "text": f"\n\n{label}: " + ", ".join(missing)}
    log.info(
        "Vraag het Joost: %d rondes, %d opzoekingen, $%.4f, vandaag $%.3f van $%.2f",
        round_ + 1, sum(lookups.values()), cost, day.spent, _budget(),
    )
    # How many questions are left today, so the page can warn before the last one.
    yield {"type": "done", "over": max(0, per_day() - day.asked.get(visitor, 0)), "gezocht": looked}


def _counts(question: str) -> tuple[int, int]:
    """How many small Dutch and English words a question has."""
    words = re.findall(r"[a-z']+", question.lower())
    return sum(word in DUTCH_WORDS for word in words), sum(word in ENGLISH_WORDS for word in words)


def _in_english(question: str, english: bool) -> bool:
    """Whether the answer is in English: for an English question, and in the English chat for any question that is
    not Dutch."""
    nl, en = _counts(question)
    return en > nl or (english and nl <= en)


def _nudged(messages: list[dict[str, Any]], nudge: str) -> list[dict[str, Any]]:
    """The same conversation with a line for Claude after the question or the results it ends with."""
    content = messages[-1]["content"]
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    return [*messages[:-1], {"role": "user", "content": [*blocks, {"type": "text", "text": nudge}]}]


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


def _card(content: str) -> dict[str, Any] | None:
    """A trein_reis result as the page shows it: cards above Joost's answer with the best trip and at most two others.
    The sums are done here, with the thresholds of ns.py, so the page needs no rules of its own and the cards say what
    Joost was told. All times are as expected now. None when there is nothing to show."""
    try:
        found = json.loads(content)
    except ValueError:
        return None
    if not isinstance(found, dict):
        return None
    trips = [trip for trip in found.get("reisadviezen") or []
             if isinstance(trip, dict) and _clock(trip.get("vertrek")) is not None and _clock(trip.get("aankomst")) is not None]
    searched = SEARCHED.fullmatch(found.get("gezocht_op") or "")
    if not trips or not searched:
        return None
    kind, day, at, last_day = searched.groups()
    kind = kind or "laatste"
    card: dict[str, Any] = {"van": _place(found.get("van")), "naar": _place(found.get("naar")), "soort": kind,
                            "datum": day or last_day}
    # The trains go between these stations. A new card for them takes the place of the old one on the page, also when
    # Joost searched again with the station instead of the place, as for an earlier train for the Erasmus MC.
    first_stretch, last_stretch = (found.get(key) if isinstance(found.get(key), dict) else {}
                                   for key in ("eerste_stuk", "laatste_stuk"))
    card["route"] = (f"{_place(first_stretch.get('naar') or found.get('van'))} > "
                     f"{_place(last_stretch.get('van') or found.get('naar'))}")
    asked = None
    if kind in ("aankomst", "vertrek"):
        # The time the visitor gave, not the one the tool searched with: that is earlier by the walk and the margin,
        # or later by the walk to the station. Only from the result, never from what Haiku filled in.
        requested = SEARCHED.fullmatch(found.get("gevraagd") or "")
        card["tijd"] = requested[3] if requested and requested[1] == kind else at
        asked = card["tijd"] if kind == "aankomst" else None
    # A stretch to or from a station that is too far to walk is not in the times, and the card says so, with the room
    # the train leaves for a bus or tram.
    for key, side in (("eerste_stuk", "begin"), ("laatste_stuk", "eind")):
        stretch = found.get(key)
        if isinstance(stretch, dict) and stretch.get("niet_berekend") and isinstance(stretch.get("meter"), int | float):
            slack = _whole(stretch.get("speling_min"))
            card.setdefault("niet_berekend", []).append(
                {"waar": side, "van": _place(stretch.get("van")), "naar": _place(stretch.get("naar")),
                 "meter": stretch["meter"], **({"speling": slack} if slack else {})}
            )
    if found.get("dienstregeling"):
        # A later day: the timetable, without delays.
        card["later"] = True
    if found.get("reisplanner"):
        # Planned door to door with bus or tram: the card names Transitous and OpenStreetMap, for the walks.
        card["bron"] = "Transitous"
    first, others = (trips[:1], trips[1:]) if trips[0].get("beste") or trips[0].get("laatste") else ([], trips)
    # The others are in time order. For an arrival search and the last train the ones nearest the asked time matter
    # most, otherwise the first ones after it.
    room = CARD_TRIPS - len(first)
    others = others[:room] if kind == "vertrek" or not first else others[-room:]
    card["reizen"] = [_card_trip(trip, asked) for trip in first + others]
    return card


def _card_trip(trip: dict[str, Any], asked: str | None) -> dict[str, Any]:
    parts = [_card_part(part) for part in trip.get("delen") or [] if isinstance(part, dict)]
    link = trip.get("link")
    found: dict[str, Any] = {
        "vertrek": trip["vertrek"],
        "aankomst": trip["aankomst"],
        **{flag: True for flag, key in (("beste", "beste"), ("laatste", "laatste"), ("nu", "vertrekt_nu")) if trip.get(key)},
        # For an arrival search: how many minutes before the asked time you are there, walk included.
        **({"over": _gap(trip["aankomst"], asked)} if asked else {}),
        "reistijd": _whole(trip.get("reistijd_min")),
        "overstappen": _card_changes(parts),
        # Of the train part only in a trip of the planner, and the card says so (see price() in index.html).
        "drukte": next((trip[key] for key in ("drukte", "drukte_trein") if isinstance(trip.get(key), str)), None),
        "prijs": next((trip[key] for key in ("prijs_euro", "prijs_trein_euro") if isinstance(trip.get(key), int | float)),
                      None),
        **({"toeslag": trip["toeslag_euro"]} if isinstance(trip.get("toeslag_euro"), int | float) else {}),
        # "normaal" goes without saying.
        **({"status": trip["status"]} if isinstance(trip.get("status"), str) and trip["status"] != "normaal" else {}),
        "delen": parts,
        "meldingen": [text[:300] for text in trip.get("meldingen") or [] if isinstance(text, str)][:4],
        # Only a link to NS itself.
        **({"link": link} if isinstance(link, str) and link.startswith(NS_LINK) else {}),
    }
    return found


def _card_part(part: dict[str, Any]) -> dict[str, Any]:
    if "lopen_min" in part:
        return {"lopen": _whole(part.get("lopen_min")), "van": _place(part.get("van")), "vertrek": part.get("vertrek"),
                "naar": _place(part.get("naar")), "aankomst": part.get("aankomst"),
                # Walking from one ride to the next in a plan: that is the transfer itself (see _card_changes).
                **({"overstap": True} if part.get("overstap") is True else {})}
    # The tool gives the departure as planned, with the delay apart; the card shows when the train leaves.
    late = _whole(part.get("vertraging_min")) or 0
    leaves = _clock(part.get("vertrek"))
    # A bus, tram, metro or ferry from the journey planner, with its line and stop, or else a train.
    vehicle = part.get("soort") in VEHICLES
    return {
        **({"soort": part["soort"], "lijn": str(part.get("lijn") or "")[:12] or None} if vehicle
           else {"trein": re.sub(r"^NS\s+", "", part.get("trein") or "") or None}),
        "richting": _place(part.get("richting")),
        "van": _place(part.get("van")),
        "vertrek": f"{(leaves + late) // 60 % 24:02d}:{(leaves + late) % 60:02d}" if leaves is not None else None,
        **({"vertraging": late} if late > 0 else {}),
        **({"perron": part["perron"] if isinstance(part.get("perron"), str) else None} if vehicle
           else {"spoor": part.get("spoor") if isinstance(part.get("spoor"), str) else None}),
        "naar": _place(part.get("naar")),
        "aankomst": part.get("aankomst"),
        **({"tussenstops": part["tussenstops"]} if isinstance(part.get("tussenstops"), int) else {}),
        **({"geannuleerd": True} if part.get("geannuleerd") else {}),
    }


def _card_changes(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Where you change trains, the minutes you have there and what that is, as ns.py says it: a walk in between comes
    off the minutes, as from Rotterdam Zuid to Rotterdam Stadion. In a plan the walk from the train to the tram is the
    transfer itself, as getting from one platform to another is for NS, so it stays in (see reisplanner._advice)."""
    changes: list[dict[str, Any]] = []
    last, walk = None, 0
    for part in parts:
        if "lopen" in part:
            walk += 0 if part.get("overstap") else part.get("lopen") or 0
            continue
        if last is not None and _clock(last.get("aankomst")) is not None and _clock(part.get("vertrek")) is not None:
            gap = _gap(last["aankomst"], part["vertrek"]) - walk
            verdict = "mis" if gap < 0 else "krap" if gap < ns.TIGHT else "genoeg" if gap < ns.AMPLE else "ruim"
            changes.append({"station": last.get("naar"), "min": gap, "oordeel": verdict})
        last, walk = part, 0
    return changes


def _clock(value: Any) -> int | None:
    """A time like 06:44 in minutes after midnight."""
    found = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value) if isinstance(value, str) else None
    return int(found[1]) * 60 + int(found[2]) if found else None


def _gap(start: str, end: str) -> int:
    """The minutes from one time to the next, also past midnight, and less than zero when the second is earlier."""
    gap = ((_clock(end) or 0) - (_clock(start) or 0)) % 1440
    return gap - 1440 if gap > 720 else gap


def _whole(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return round(value)
    return int(value) if isinstance(value, str) and value.strip().isdigit() else None


def _place(name: Any) -> str | None:
    """A station without the sign NS puts after some, as in "Schiphol Airport ✈"."""
    if not isinstance(name, str):
        return None
    return re.sub(r"[^\w).']+$", "", name).strip() or None


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
    # On a Saturday Haiku took "volgende week dinsdag" for 3 november instead of 27 oktober.
    monday = now.date() + timedelta(days=7 - now.weekday())
    sunday = monday + timedelta(days=6)
    return (
        f"Nu is het {DAYS[now.weekday()]} {now.day} {MONTHS[now.month - 1]} {now.year} "
        f"({now:%Y-%m-%d}), {now:%H:%M} uur Nederlandse tijd. Volgende week loopt van maandag {monday.day} "
        f"{MONTHS[monday.month - 1]} tot en met zondag {sunday.day} {MONTHS[sunday.month - 1]}."
    )

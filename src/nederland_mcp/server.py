"""The Hey Joost MCP server: Dutch public data as tools for AI assistants, plus its website."""

import base64
import html
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import Icon, ToolAnnotations
from pydantic import BeforeValidator, Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from . import __version__, tellers, veilig, vraag
from .bronnen import bekendmakingen as bekendmakingbron
from .bronnen import (
    buurt,
    huis,
    knmi,
    ns,
    ov,
    reisplanner,
    schiphol,
    stroom,
    vakanties,
    water,
)
from .bronnen import regelzaken as regelzaakbron
from .bronnen import reisadvies as reisadviezen
from .bronnen import verkeer as verkeersbron
from .bronnen import weer as weerbron
from .bronnen import winkels as winkelbron

WEBSITE = "https://heyjoost.nl"
SITE = Path(__file__).parent / "site"

# Written as facts, not orders: directories reject servers that tell the assistant how to behave.
INSTRUCTIONS = (
    "Hey Joost geeft actuele Nederlandse open data uit officiële bronnen: adressen en gebouwen uit de BAG, "
    "cijfers over buurten van het CBS, schoolvakanties, waterstanden, watertemperatuur en eb en vloed, het weer, "
    "de regenradar en de weerwaarschuwingen van het KNMI, de stroomprijs per kwartier, het reisadvies van "
    "Buitenlandse Zaken, officiële antwoorden over regelzaken van de Rijksoverheid en gemeenten, bekendmakingen "
    "zoals vergunningen rond een adres, het verkeer op de weg van NDW, supermarkten, apotheken en pinautomaten in "
    "de buurt met openingstijden uit OpenStreetMap, NS-treinen met storingen en werkzaamheden, en bus, tram en "
    "metro.\n"
    "- Elk resultaat heeft een veld 'bron' met de herkomst van de data, voor de bronvermelding.\n"
    "- Alle tijden zijn Nederlandse tijd (Europe/Amsterdam).\n"
    "- De data bevat geen persoonsgegevens. De tools zijn niet bedoeld om mensen op te zoeken."
)

class HeyJoost(MCPServer):
    """Counts every tool call, through MCP and from the chat alike (the chat marks its own, see tellers.via)."""

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        tellers.tool(name)
        return await super().call_tool(name, arguments, context)


server = HeyJoost(
    name="heyjoost",
    title="Hey Joost",
    description=(
        "Live Nederland in je AI: treinen en storingen, bus en tram, files en werk aan de weg, weer en regenradar, "
        "eb en vloed, de stroomprijs, buurten, vergunningen in je straat, wat er open is in de buurt, "
        "schoolvakanties, huizen, reisadvies en regelzaken, uit officiële en open bronnen."
    ),
    instructions=INSTRUCTIONS,
    website_url=WEBSITE,
    # Inline, so the icon works on any host and before the domain is live.
    icons=[Icon(
        src="data:image/svg+xml;base64," + base64.b64encode((SITE / "favicon.svg").read_bytes()).decode(),
        mime_type="image/svg+xml",
        sizes=["any"],
    )],
    version=__version__,
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

# Assistants sometimes send "Noord" or "Beide"; accept that instead of failing on the enum.
LOWER = BeforeValidator(lambda value: value.strip().lower() if isinstance(value, str) else value)


@server.tool(
    title="Huis-check",
    description=(
        "Feiten over een Nederlands adres uit de BAG (Kadaster): bouwjaar, oppervlakte, gebruiksdoel, wijk en "
        "buurt. Werkt met straat, huisnummer en plaats: de tool zoekt het adres en de postcode zelf op, dus een "
        "postcode is niet nodig. Met postcode en huisnummer werkt het ook. Bevat geen persoonsgegevens en geen "
        "WOZ-waarde."
    ),
    annotations=READ_ONLY,
)
async def huis_info(
    *,
    straat: Annotated[str | None, Field(description="Straatnaam, bijvoorbeeld Rapenburg")] = None,
    huisnummer: Annotated[int, Field(ge=1, description="Huisnummer zonder letter of toevoeging")],
    plaats: Annotated[str | None, Field(description="Woonplaats, bijvoorbeeld Leiden")] = None,
    toevoeging: Annotated[str | None, Field(description="Huisletter of toevoeging, bijvoorbeeld A of 2")] = None,
    postcode: Annotated[str | None, Field(description="Alleen als de gebruiker een postcode noemt, bijvoorbeeld 2311EZ")] = None,
) -> dict[str, Any]:
    return await huis.huis_info(huisnummer, postcode, straat, plaats, toevoeging)


@server.tool(
    title="Buurtprofiel",
    description=(
        "Cijfers van het CBS over een buurt, wijk of gemeente: inwoners en leeftijden, huishoudens, woningen met de "
        "gemiddelde WOZ-waarde en het aandeel koop- en huurwoningen, inkomen, energieverbruik en zonnepanelen, de "
        "afstand tot huisarts, supermarkt en school, en auto's. Met de gemeente en heel Nederland ernaast om te "
        "vergelijken, en per onderdeel het jaar van de cijfers. Werkt met een buurt, wijk, plaats, straat met "
        "plaats of postcode. Voor een gebied dat het CBS niet als één geheel telt, zoals de Bijlmer, Haren of "
        "Kralingen, komen de wijken of buurten naast elkaar. Het zijn gemiddelden voor het hele gebied, nooit over "
        "één huis of persoon."
    ),
    annotations=READ_ONLY,
)
async def buurt_info(
    plaats: Annotated[str, Field(description="Buurt, wijk, plaats, straat met plaats of postcode, bijvoorbeeld Vogelwijk Den Haag, Rapenburg Leiden of 2311EZ")],
) -> dict[str, Any]:
    return await buurt.buurt_info(plaats)


@server.tool(
    title="Schoolvakanties",
    description=(
        "Officiële schoolvakanties in Nederland per regio (noord, midden, zuid), van de Rijksoverheid. "
        "Zonder schooljaar krijg je de komende vakanties. Welke regio bij een gemeente hoort staat op de "
        "pagina in 'regio_per_gemeente'."
    ),
    annotations=READ_ONLY,
)
async def schoolvakanties(
    regio: Annotated[Literal["noord", "midden", "zuid"] | None, LOWER, Field(description="Vakantieregio. Leeg = alle regio's")] = None,
    schooljaar: Annotated[str | None, Field(description="Schooljaar, bijvoorbeeld 2026-2027. Leeg = de komende vakanties")] = None,
) -> dict[str, Any]:
    return await vakanties.schoolvakanties(regio, schooljaar)


@server.tool(
    title="Waterstand en watertemperatuur",
    description=(
        "Actuele waterstand (ten opzichte van NAP) en watertemperatuur bij een plaats in Nederland, gemeten "
        "door Rijkswaterstaat. Werkt het best voor plaatsen aan de kust, grote rivieren en meren. Geeft de "
        "dichtstbijzijnde meetpunten met afstand en meettijd. Eb en vloed en de verwachte waterstand staan in Eb en "
        "vloed."
    ),
    annotations=READ_ONLY,
)
async def water_actueel(
    plaats: Annotated[str, Field(description="Plaats of meetpunt, bijvoorbeeld Scheveningen, Hoek van Holland of Lobith")],
    soort: Annotated[Literal["waterstand", "watertemperatuur", "beide"], LOWER, Field(description="Wat je wilt weten")] = "beide",
) -> dict[str, Any]:
    return await water.water_actueel(plaats, soort)


@server.tool(
    title="Eb en vloed",
    description=(
        "Hoogwater en laagwater (eb en vloed) bij een plaats aan de Nederlandse kust, in Zeeland, op de Waddenzee "
        "en aan de grote rivieren, van Rijkswaterstaat. Zonder datum krijg je vandaag en morgen, met een datum die "
        "dag, tot een jaar vooruit. Voor vandaag en morgen rekent de verwachting de wind en de rivieren mee "
        "('volgens': 'verwachting'), met de getijtabel ernaast; daarna is het de getijtabel. Hoogtes zijn in cm "
        "ten opzichte van NAP. Bij plaatsen zonder eb en vloed, zoals aan de Maas of het IJsselmeer, krijg je de "
        "verwachte waterstand voor de komende twee dagen, met de hoogste en laagste stand. 'meetpunt' en "
        "'afstand_km' zeggen waar het gemeten wordt."
    ),
    annotations=READ_ONLY,
)
async def water_verwachting(
    plaats: Annotated[str, Field(description="Plaats of meetpunt, bijvoorbeeld Scheveningen, Harlingen, Vlissingen of Venlo")],
    datum: Annotated[str | None, Field(description="Datum als JJJJ-MM-DD. Leeg = vandaag en morgen")] = None,
) -> dict[str, Any]:
    return await water.water_verwachting(plaats, datum)


@server.tool(
    title="Weer",
    description=(
        "Het weer op een plek in Nederland: nu, per uur voor de komende 24 uur en per dag voor de komende week. "
        "Met temperatuur, neerslag in millimeters, wind in Beaufort en windstoten. Met een datum krijg je die dag "
        "per uur. Uit de weermodellen van het KNMI (HARMONIE-AROME) en ECMWF, via Open-Meteo. Geldt er voor die "
        "plek een weerwaarschuwing van het KNMI, dan staat die in 'let_op'. Of het de komende twee uur gaat "
        "regenen, staat precies in Regenradar."
    ),
    annotations=READ_ONLY,
)
async def weer(
    plaats: Annotated[str, Field(description="Plaats, wijk, station of postcode, bijvoorbeeld Leiden, Scheveningen of Utrecht Centraal")],
    datum: Annotated[str | None, Field(description="Datum als JJJJ-MM-DD, tot 6 dagen vooruit. Leeg = nu, de komende 24 uur en de week")] = None,
) -> dict[str, Any]:
    return await weerbron.weer(plaats, datum)


@server.tool(
    title="Regenradar",
    description=(
        "Regen op een plek in Nederland voor de komende twee uur, per tien minuten, uit de neerslagradar van het "
        "KNMI. Zegt of het droog blijft, wanneer de regen begint of stopt en hoe hard het regent, in millimeter "
        "per uur. De 'samenvatting' zet dat in één zin. Voor later dan twee uur is er Weer."
    ),
    annotations=READ_ONLY,
)
async def regen(
    plaats: Annotated[str, Field(description="Plaats, wijk of postcode, bijvoorbeeld Leiden, Scheveningen of 2311")],
) -> dict[str, Any]:
    return await knmi.regen(plaats)


@server.tool(
    title="Weerwaarschuwingen",
    description=(
        "De weerwaarschuwingen van het KNMI: code geel, oranje of rood, voor wat (zoals windstoten, gladheid of "
        "onweersbuien), waar en van wanneer tot wanneer, met de uitleg van het KNMI. Met een plaats of provincie "
        "krijg je de waarschuwingen die daar gelden, en in 'elders_in_nederland' de rest van het land. Zonder "
        "plaats krijg je alles. Waarschuwingen gaan tot 48 uur vooruit."
    ),
    annotations=READ_ONLY,
)
async def weerwaarschuwingen(
    plaats: Annotated[str | None, Field(description="Plaats, wijk, postcode of provincie, bijvoorbeeld Leiden of Zeeland. Leeg = heel Nederland")] = None,
) -> dict[str, Any]:
    return await knmi.waarschuwingen(plaats)


@server.tool(
    title="Stroomprijs",
    description=(
        "De stroomprijs per kwartier in Nederland, zoals je die met een dynamisch contract betaalt: de prijs op de "
        "stroombeurs (day-ahead), kaal en met energiebelasting en btw, in cent per kWh. Zonder datum krijg je nu, "
        "vandaag en morgen (die van morgen pas een stuk na de veiling van 12:00), met de goedkoopste momenten vanaf nu: het "
        "goedkoopste kwartier, uur en blok van drie uur. Met een datum krijg je die dag, tot een maand terug. De "
        "opslag van de leverancier, vaste kosten en netbeheerkosten zitten er niet in."
    ),
    annotations=READ_ONLY,
)
async def stroomprijs(
    datum: Annotated[str | None, Field(description="Datum als JJJJ-MM-DD, van een maand terug tot morgen. Leeg = nu, vandaag en morgen")] = None,
) -> dict[str, Any]:
    return await stroom.stroomprijs(datum)


@server.tool(
    title="Reisadvies",
    description=(
        "Het officiële reisadvies van het ministerie van Buitenlandse Zaken voor een land in het buitenland: de "
        "kleurcode (groen, geel, oranje of rood) en voor welk gebied die geldt, wat er speelt en wanneer het advies "
        "voor het laatst is gewijzigd, met de link. Met een onderwerp komt ook de tekst daarover mee, zoals paspoort "
        "en visum, vaccinaties, criminaliteit, wetten en gebruiken (drugs, alcohol, kleding) of wat je doet in geval "
        "van nood. 'onderwerpen' noemt wat er verder in het advies staat. Werkt met het land of een bekende plek, "
        "zoals Mallorca, Bali of Dubai. Niet voor een reis met trein, bus of tram: dat zijn trein_reis en ov_vertrek."
    ),
    annotations=READ_ONLY,
)
async def reisadvies(
    land: Annotated[str, Field(description="Land of bekende plek, bijvoorbeeld Turkije, Verenigde Staten of Mallorca")],
    onderwerp: Annotated[str | None, Field(description="Waar de vraag over gaat, bijvoorbeeld visum, vaccinaties, veiligheid, drugs of ambassade. Leeg = alleen de kleurcode en het korte overzicht")] = None,
) -> dict[str, Any]:
    return await reisadviezen.reisadvies(land, onderwerp)


@server.tool(
    title="Regelzaken",
    description=(
        "Officiële antwoorden over regelzaken met de overheid, uit de vragen en antwoorden van Rijksoverheid.nl: "
        "toeslagen, AOW, kinderbijslag, paspoort en ID-kaart, rijbewijs, huren, zorgverzekering, werk, belasting en "
        "meer, met de link naar de pagina. Met een gemeente komen ook de producten van die gemeente mee, zoals een "
        "paspoort aanvragen, een parkeervergunning, grofvuil of een verhuizing doorgeven, met de link naar de site van "
        "de gemeente en of het online kan. Het is de algemene uitleg van de overheid, geen advies over iemands eigen "
        "situatie."
    ),
    annotations=READ_ONLY,
)
async def regelzaken(
    vraag: Annotated[str, Field(description="De vraag of het onderwerp, bijvoorbeeld 'wanneer moet ik mijn paspoort verlengen' of 'huurtoeslag'")],
    gemeente: Annotated[str | None, Field(description="Gemeente of plaats, alleen als het om iets bij de gemeente gaat, bijvoorbeeld Leiden of Scheveningen. Leeg = alleen de Rijksoverheid")] = None,
) -> dict[str, Any]:
    return await regelzaakbron.regelzaken(vraag, gemeente)


@server.tool(
    title="Bekendmakingen in de buurt",
    description=(
        "Officiële bekendmakingen van gemeente, provincie, waterschap en Rijk rond een adres: aangevraagde en "
        "verleende vergunningen (bouwen en verbouwen, bomen kappen, evenementen, horeca), meldingen en "
        "verkeersbesluiten, met de soort, de straat, de datum, de afstand en de link naar de bekendmaking. 'fase' "
        "zegt of het een aanvraag, ontwerp of besluit is, 'reageren_tot' tot wanneer je kunt reageren als dat in de "
        "bekendmaking staat, en 'uitleg' hoe bezwaar en zienswijze werken. Zoekt alleen rond een adres, straat of "
        "postcode, tot 500 meter, niet op naam of trefwoord. Huisnummers, postcodes en bekendmakingen over personen "
        "laat het weg."
    ),
    annotations=READ_ONLY,
)
async def bekendmakingen(
    adres: Annotated[str, Field(description="Adres, straat of postcode, bijvoorbeeld Rapenburg 70 of 2311EZ")],
    plaats: Annotated[str | None, Field(description="Plaats van het adres, bijvoorbeeld Leiden")] = None,
    straal_m: Annotated[int, Field(ge=50, le=500, description="Zoekstraal rond het adres in meters")] = 250,
    weken: Annotated[int, Field(ge=1, le=26, description="Hoeveel weken terug")] = 8,
) -> dict[str, Any]:
    return await bekendmakingbron.bekendmakingen(adres, plaats, straal_m, weken)


@server.tool(
    title="Open in de buurt",
    description=(
        "Supermarkten, apotheken en pinautomaten in de buurt van een adres, straat, station, wijk of plaats, de "
        "dichtstbijzijnde eerst, met de afstand hemelsbreed in meters ('afstand_m'), het adres (of anders de straat) "
        "en de openingstijden: of het nu open is ('open_nu'), tot hoe laat ('open_tot') en wanneer het weer opengaat "
        "('gaat_open'). 'rond' zegt vanaf waar gezocht is; met alleen een plaats is dat het centrum. Met nu_open "
        "alleen wat nu open is. De openingstijden komen uit OpenStreetMap en kunnen afwijken, zeker op feestdagen. "
        "Bij een apotheek staat erbij hoe je buiten openingstijden de dienstapotheek vindt."
    ),
    annotations=READ_ONLY,
)
async def open_in_de_buurt(
    waar: Annotated[str, Field(description="Adres, straat, station, wijk of plaats, bijvoorbeeld Rapenburg 70, Utrecht Centraal of Scheveningen")],
    soort: Annotated[str, Field(description="supermarkt, apotheek of pinautomaat")] = "supermarkt",
    plaats: Annotated[str | None, Field(description="Plaats van het adres of de straat, bijvoorbeeld Leiden")] = None,
    nu_open: Annotated[bool, Field(description="Alleen wat nu open is")] = False,
    aantal: Annotated[int, Field(ge=1, le=10, description="Hoeveel plekken")] = 5,
) -> dict[str, Any]:
    return await winkelbron.open_in_de_buurt(waar, soort, plaats, nu_open, aantal)


@server.tool(
    title="Treinvertrek",
    description=(
        "Vertrektijden van treinen vanaf een station, met vertraging, spoor, spoorwijzigingen en uitval. "
        "Zonder tijd krijg je de vertrekken vanaf nu. Gaat ongeveer een uur vooruit; voor latere tijden is er "
        "Treinreis plannen."
    ),
    annotations=READ_ONLY,
)
async def trein_vertrek(
    station: Annotated[str, Field(description="Stationsnaam of code, bijvoorbeeld Leiden Centraal of LEDN")],
    aantal: Annotated[int, Field(ge=1, le=25, description="Aantal vertrekken")] = 10,
    datum: Annotated[str | None, Field(description="Datum als JJJJ-MM-DD. Leeg = vandaag")] = None,
    tijd: Annotated[str | None, Field(description="Vanaf welke tijd, als UU:MM. Leeg = nu")] = None,
) -> dict[str, Any]:
    return await ns.trein_vertrek(station, aantal, datum, tijd)


@server.tool(
    title="Treinaankomst",
    description=(
        "Aankomsttijden van treinen op een station: waar de trein vandaan komt, vertraging, spoor en uitval. "
        "Handig als je iemand van de trein ophaalt. Zonder tijd krijg je de aankomsten vanaf nu. Gaat ongeveer "
        "een uur vooruit; voor latere tijden is er Treinreis plannen. Rijdt een trein door, dan heeft hij bij "
        "Treinvertrek hetzelfde treinnummer."
    ),
    annotations=READ_ONLY,
)
async def trein_aankomst(
    station: Annotated[str, Field(description="Stationsnaam of code, bijvoorbeeld Leiden Centraal of LEDN")],
    aantal: Annotated[int, Field(ge=1, le=25, description="Aantal aankomsten")] = 10,
    datum: Annotated[str | None, Field(description="Datum als JJJJ-MM-DD. Leeg = vandaag")] = None,
    tijd: Annotated[str | None, Field(description="Vanaf welke tijd, als UU:MM. Leeg = nu")] = None,
) -> dict[str, Any]:
    return await ns.trein_aankomst(station, aantal, datum, tijd)


@server.tool(
    title="Treinreis plannen",
    description=(
        "Reisadvies met de trein tussen twee stations: vertrek, aankomst, overstappen, vertraging, drukte en "
        "prijs. Zonder datum en tijd wordt er gezocht vanaf nu. De beste reis staat bovenaan, met beste=true: die "
        "komt zo vroeg mogelijk aan met zo min mogelijk overstappen (een overstap telt als 5 minuten, een krappe "
        "als 10). Met aankomst=true komen alleen reizen die op tijd aankomen, en vertrekt de beste zo laat "
        "mogelijk; met lopen_min gaat daar eerst het laatste stuk en 5 minuten marge af, en staat bij elke reis in "
        "'op_bestemming' hoe laat je er dan bent. Daarna volgen de andere "
        "reizen op tijd; een reis die in alles slechter is dan een andere valt weg. 'overstaptijd' zegt per "
        "overstap hoeveel minuten je hebt en of dat krap (minder dan 5), genoeg of ruim (vanaf 10) is, en "
        "'tussenstops' per deel hoe vaak de trein onderweg stopt. 'dag' is de dag in woorden, zoals 'maandag 26 "
        "oktober (over 3 dagen)'. Een trein die nu of binnen 2 minuten vertrekt, heeft 'vertrekt_nu': die haal je "
        "alleen als je er al bent, dus die is niet de beste zolang er een andere is. Treinen die al weg zijn, vallen "
        "weg. Met laatste=true komen de laatste treinen van die dag en telt alleen de datum; een trein tot 4 uur 's "
        "nachts hoort nog bij die avond. De laatste staat dan bovenaan met laatste=true, en treinen die na 1 uur 's "
        "nachts vertrekken, staan in een zin in 'nachttreinen'. Bij de Intercity direct en de ICE staat de toeslag "
        "in 'toeslag_euro', en die zit al in de prijs. Reizen die niet gaan, omdat een trein uitvalt of een overstap niet meer lukt, staan in "
        "'niet_mogelijk'. Rijdt er door werkzaamheden of een storing een bus in plaats van de trein, dan staat het "
        "bericht van NS in 'meldingen'."
    ),
    annotations=READ_ONLY,
)
async def trein_reis(
    van: Annotated[str, Field(description="Vertrekstation, bijvoorbeeld Den Haag Centraal")],
    naar: Annotated[str, Field(description="Aankomststation, bijvoorbeeld Utrecht Centraal")],
    datum: Annotated[str | None, Field(description="Datum als JJJJ-MM-DD. Leeg = vandaag")] = None,
    tijd: Annotated[str | None, Field(description="Tijd als UU:MM. Leeg = nu")] = None,
    aankomst: Annotated[bool, Field(description="True als de tijd de gewenste aankomsttijd is")] = False,
    lopen_min: Annotated[int | None, Field(ge=0, le=120, description="Alleen met aankomst=true: hoeveel minuten je na de trein nog onderweg bent naar waar je op tijd moet zijn, zoals lopen of de tram, en 0 als dat het station zelf is. De tool haalt dat en 5 minuten marge van de tijd af. Leeg = precies op de tijd aankomen")] = None,
    laatste: Annotated[bool, Field(description="True voor de laatste trein van die dag, of om zo laat mogelijk te vertrekken. Dan telt alleen de datum, niet tijd, aankomst of lopen_min")] = False,
) -> dict[str, Any]:
    return await ns.trein_reis(van, naar, datum, tijd, aankomst, lopen_min, laatste)


@server.tool(
    title="Storingen en werkzaamheden",
    description=(
        "Storingen en werkzaamheden op het spoor, van NS: wat er aan de hand is, waar, van wanneer tot wanneer, "
        "of er bussen rijden en het reisadvies van NS. Zonder datum krijg je de storingen van nu, de werkzaamheden "
        "van nu en die van de komende drie dagen. Met datum (en tot) de werkzaamheden in die periode, tot 31 dagen "
        "tegelijk. Met een station alleen wat op het spoor langs dat station speelt, en waar de vervangende bus "
        "daar vertrekt. Landelijke berichten, zoals een staking, staan in 'landelijke_berichten'. Voor een reis "
        "van A naar B op een bepaald moment is er ook Treinreis plannen."
    ),
    annotations=READ_ONLY,
)
async def trein_storingen(
    station: Annotated[str | None, Field(description="Stationsnaam, bijvoorbeeld Leiden Centraal of Utrecht. Leeg = heel Nederland")] = None,
    datum: Annotated[str | None, Field(description="Eerste dag als JJJJ-MM-DD. Leeg = nu en de komende dagen")] = None,
    tot: Annotated[str | None, Field(description="Laatste dag als JJJJ-MM-DD, voor een periode zoals een weekend. Leeg = alleen de datum")] = None,
) -> dict[str, Any]:
    return await ns.trein_storingen(station, datum, tot)


@server.tool(
    title="Bus, tram en metro",
    description=(
        "Vertrektijden van bus, tram, metro en veerboot vanaf een halte in Nederland, met live vertraging en "
        "uitval als de vervoerder die doorgeeft. Werkt met de naam van de halte en de plaats, bijvoorbeeld Spui "
        "in Den Haag of Leidseplein in Amsterdam, en ook met een adres, straat of postcode, of een plek zonder "
        "eigen halte zoals het LUMC of Ahoy: dan komen de drie dichtstbijzijnde haltes mee in "
        "'haltes_in_de_buurt', met de afstand hemelsbreed in meters ('afstand_m'). Bij een treinstation komen alle "
        "bus- en tramhaltes rond het station mee, elk met halte en perron. Bij een plek die zijn naam aan één "
        "halte geeft, zoals het Erasmus MC, komen ook de haltes eromheen mee, elk met 'afstand_m'. Zonder tijd "
        "krijg je de vertrekken vanaf nu; met datum en tijd ook later, volgens de dienstregeling. Met naar krijg "
        "je alleen de bussen en trams die daarna bij die halte stoppen, met de aankomsttijd daar ('stopt_bij' en "
        "'aankomst'), ook vanaf een adres, bijvoorbeeld naar het station. Vanaf een gewone halte tellen dan ook de "
        "haltes om de hoek mee, met 'afstand_m' en alleen wat je lopend nog haalt. Is naar zo'n plek, of een plek "
        "zonder eigen halte, dan tellen ook de haltes op loopafstand ervan: dan staat er hoe lang je daarna nog "
        "loopt ('lopen_min') en hoe laat je er bent ('op_bestemming'). Ligt de plek op loopafstand, dan staat in "
        "'lopen' hoe lang het lopen is. Een negatieve vertraging betekent te vroeg. "
        "'andere_haltes' noemt haltes met een andere naam die ook gevonden zijn. Bij een staking of een andere "
        "grote verstoring van bus, tram of metro staat er een bericht van NS in 'let_op', want de dienstregeling "
        "toont dan nog alle ritten. Treinen staan in Treinvertrek."
    ),
    annotations=READ_ONLY,
)
async def ov_vertrek(
    halte: Annotated[str, Field(description="Naam van de halte, bijvoorbeeld Spui, Leidseplein of Leiden Centraal. Mag ook een adres, straat, postcode of plek zijn, bijvoorbeeld Laan van Meerdervoort 100 of LUMC")],
    plaats: Annotated[str | None, Field(description="Plaats of wijk van de halte of het adres, bijvoorbeeld Den Haag of Scheveningen")] = None,
    aantal: Annotated[int, Field(ge=1, le=25, description="Aantal vertrekken")] = 10,
    lijn: Annotated[str | None, Field(description="Alleen deze lijn of soort, bijvoorbeeld 2, tram 2, 400, E of metro")] = None,
    naar: Annotated[str | None, Field(description="Alleen vertrekken die daarna bij deze halte of plek stoppen, bijvoorbeeld Leiden Centraal, Kurhaus of Erasmus MC")] = None,
    datum: Annotated[str | None, Field(description="Datum als JJJJ-MM-DD. Leeg = vandaag")] = None,
    tijd: Annotated[str | None, Field(description="Vanaf welke tijd, als UU:MM. Leeg = nu")] = None,
) -> dict[str, Any]:
    return await ov.ov_vertrek(halte, plaats, aantal, lijn, datum, tijd, naar)


# Only once Transitous agrees to routing (see bronnen/reisplanner.py): HEYJOOST_REISPLANNER=1.
if reisplanner.AAN:

    @server.tool(
        title="Reis plannen",
        description=(
            "Van deur tot deur met het ov in Nederland: lopen, bus, tram, metro, trein en veerboot, vanaf een adres, "
            "straat, station of plaats naar een ander, met de beste drie reizen. Per reis de vertrek- en "
            "aankomsttijd, de reistijd en het aantal overstappen, en per deel de lijn en richting, waar je instapt "
            "en uitstapt, het spoor of perron, en hoe lang je loopt. Met datum en tijd voor later; met aankomst=true "
            "is de tijd de aankomsttijd. Prijs en drukte van de trein staan in Treinreis."
        ),
        annotations=READ_ONLY,
    )
    async def reis_plannen(
        van: Annotated[str, Field(description="Vertrek: adres, straat, station of plaats, bijvoorbeeld Rapenburg 70 of Utrecht Centraal")],
        naar: Annotated[str, Field(description="Bestemming: adres, straat, station of plaats, bijvoorbeeld Kurhaus of Binnenhof 1")],
        van_plaats: Annotated[str | None, Field(description="Plaats van het vertrekadres, bijvoorbeeld Leiden")] = None,
        naar_plaats: Annotated[str | None, Field(description="Plaats van de bestemming, bijvoorbeeld Den Haag")] = None,
        datum: Annotated[str | None, Field(description="Datum als JJJJ-MM-DD. Leeg = vandaag")] = None,
        tijd: Annotated[str | None, Field(description="Tijd als UU:MM. Leeg = nu")] = None,
        aankomst: Annotated[bool, Field(description="true = de tijd is de aankomsttijd")] = False,
    ) -> dict[str, Any]:
        return await reisplanner.reis_plannen(van, naar, van_plaats, naar_plaats, datum, tijd, aankomst)


@server.tool(
    title="Verkeer",
    description=(
        "Het verkeer op de weg nu, uit het actuele beeld van NDW (Nationaal Dataportaal Wegverkeer): files, "
        "ongelukken en pech, afgesloten wegen, afritten en rijstroken, werkzaamheden, open bruggen en lagere "
        "snelheden, met waar het is (weg, afrit en richting, of straat en plaats), de lengte van een file, sinds en "
        "tot wanneer. Met een plaats de berichten in de buurt, met een weg (A12, N206) die weg. Zonder plaats en weg "
        "van elk wat: files, ongelukken en afsluitingen op de grote wegen in heel Nederland. Met 'soort' alleen die "
        "soort, zoals 'afsluitingen' voor de wegen die dicht zijn, met 'per_weg' waar de rest is. De berichten zijn "
        "niet gecontroleerd en niet elke wegbeheerder meldt alles, zie 'let_op'."
    ),
    annotations=READ_ONLY,
)
async def verkeer(
    waar: Annotated[str | None, Field(description="Plaats of wijk, bijvoorbeeld Utrecht of Scheveningen. Leeg = heel Nederland of de hele weg")] = None,
    weg: Annotated[str | None, Field(description="Wegnummer, bijvoorbeeld A12 of N206")] = None,
    straal_km: Annotated[float | None, Field(ge=1, le=25, description="Zoekstraal rond de plaats in km. Leeg = 8 km")] = None,
    soort: Annotated[
        Literal["files", "ongelukken_en_pech", "afsluitingen", "werkzaamheden", "snelheid"] | None, LOWER,
        Field(description="Alleen deze soort berichten, zoals afsluitingen als iemand vraagt welke wegen dicht zijn. Leeg = alles"),
    ] = None,
) -> dict[str, Any]:
    return await verkeersbron.verkeer(waar, weg, straal_km, soort)


# The home page shows the MCP URL of the host it was reached on, so a local run shows its own address.
HOST = re.compile(r"(?P<name>[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(?::\d{1,5})?")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
# Flights are only in the chat on this site (see bronnen/schiphol.py), so the pages only mention them when the
# chat can look them up. <!--vluchten--> and <!--/vluchten--> mark those parts. The journey planner is off until
# Transitous agrees (see bronnen/reisplanner.py), and <!--reisplanner--> marks what the pages say about it.
FLIGHTS = re.compile(r"<!--vluchten-->(.*?)<!--/vluchten-->", re.S)
PLANNER = re.compile(r"<!--reisplanner-->(.*?)<!--/reisplanner-->", re.S)


def _flights(page: str, keep: bool) -> str:
    return FLIGHTS.sub(lambda match: match[1] if keep else "", page)


def _planner(page: str) -> str:
    return PLANNER.sub(lambda match: match[1] if reisplanner.AAN else "", page)


# The home page is the chat, and /over says what Joost knows and how to add him to your own AI. Both show the
# flights only when the chat can look them up. The version in the stylesheet link makes browsers load a changed
# stylesheet right away.
def _page(filename: str) -> dict[bool, str]:
    page = _planner((SITE / filename).read_text(encoding="utf-8").replace("{{VERSION}}", __version__))
    return {keep: _flights(page, keep) for keep in (True, False)}


HOME = _page("index.html")
ABOUT = _page("over.html")


def _mcp_url(request: Request) -> str:
    match = HOST.fullmatch(request.headers.get("host", ""))
    if not match:
        return f"{WEBSITE}/mcp"
    scheme = "http" if match["name"].lower() in LOCAL_HOSTS else "https"
    return f"{scheme}://{match[0]}/mcp"


def _fill(pages: dict[bool, str], request: Request) -> HTMLResponse:
    page = pages[vraag.flights()].replace("{{MCP_URL}}", html.escape(_mcp_url(request)))
    page = page.replace("{{VRAAG}}", vraag.state())
    return HTMLResponse(page, headers={"Content-Security-Policy": veilig.csp(page)})


@server.custom_route("/", methods=["GET"])
async def home(request: Request) -> HTMLResponse:
    if request.method == "GET":
        tellers.tel("site")
    return _fill(HOME, request)


@server.custom_route("/over", methods=["GET"])
async def about(request: Request) -> HTMLResponse:
    return _fill(ABOUT, request)


@server.custom_route("/api/vraag", methods=["POST"])
async def ask_joost(request: Request) -> Response:
    return await vraag.ask(request, server)


def _serve(path: str, filename: str, media_type: str, max_age: int) -> None:
    body = (SITE / filename).read_bytes()
    headers = {"Cache-Control": f"public, max-age={max_age}"}
    if media_type == "text/html":
        page = _planner(_flights(body.decode(), schiphol.available())).replace("{{VERSION}}", __version__)
        body = page.encode()
        headers["Content-Security-Policy"] = veilig.csp(page)

    async def page(request: Request) -> Response:
        return Response(body, media_type=media_type, headers=headers)

    server.custom_route(path, methods=["GET"], name=filename)(page)


_serve("/privacy", "privacy.html", "text/html", 300)
_serve("/voorwaarden", "voorwaarden.html", "text/html", 300)
_serve("/toegankelijkheid", "toegankelijkheid.html", "text/html", 300)
_serve("/style.css", "style.css", "text/css", 300)
_serve("/favicon.svg", "favicon.svg", "image/svg+xml", 86400)
# A font file never changes under the same name, so browsers may keep it for a year.
_serve("/fonts/plex-sans.woff2", "fonts/plex-sans.woff2", "font/woff2", 31536000)
_serve("/fonts/plex-mono-400.woff2", "fonts/plex-mono-400.woff2", "font/woff2", 31536000)
_serve("/fonts/plex-mono-600.woff2", "fonts/plex-mono-600.woff2", "font/woff2", 31536000)
_serve("/robots.txt", "robots.txt", "text/plain", 86400)
# For "Zet Joost op je beginscherm". iPhones look for the touch icon in the root, even without a link to it.
_serve("/manifest.webmanifest", "manifest.webmanifest", "application/manifest+json", 86400)
_serve("/apple-touch-icon.png", "icons/apple-touch-icon.png", "image/png", 86400)
_serve("/icons/joost-192.png", "icons/joost-192.png", "image/png", 86400)
_serve("/icons/joost-512.png", "icons/joost-512.png", "image/png", 86400)
_serve("/icons/joost-maskable-512.png", "icons/joost-maskable-512.png", "image/png", 86400)
# The picture a link to heyjoost.nl gets on LinkedIn, X, WhatsApp and Slack. Those sites keep a picture for days,
# so a new one needs a new name.
_serve("/og.png", "og.png", "image/png", 86400)

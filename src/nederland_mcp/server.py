"""The MCP server: Dutch public data as tools for AI assistants."""

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BeforeValidator, Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from . import __version__
from .bronnen import huis, ns, schiphol, vakanties, water

INSTRUCTIONS = (
    "Deze server geeft officiële, actuele Nederlandse open data: adresgegevens uit de BAG, schoolvakanties, "
    "waterstanden en watertemperatuur, NS-treinreizen en Schiphol-vluchten.\n"
    "- Noem in je antwoord altijd de bron uit het veld 'bron'.\n"
    "- Alle tijden zijn Nederlandse tijd (Europe/Amsterdam).\n"
    "- Antwoord in de taal van de gebruiker.\n"
    "- Deze data bevat geen persoonsgegevens. Gebruik de tools niet om mensen op te zoeken."
)

server = MCPServer(
    name="nederland-mcp",
    title="Nederland MCP",
    description="Nederlandse open data voor AI-assistenten: huis, schoolvakanties, water, trein en vlucht.",
    instructions=INSTRUCTIONS,
    version=__version__,
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

# Assistants sometimes send "Noord" or "Beide"; accept that instead of failing on the enum.
LOWER = BeforeValidator(lambda value: value.strip().lower() if isinstance(value, str) else value)


@server.tool(
    title="Huis-check",
    description=(
        "Feiten over een Nederlands adres uit de BAG (Kadaster): bouwjaar, oppervlakte, gebruiksdoel, wijk, "
        "buurt en, als het geregistreerd is, het energielabel. Geef een postcode met huisnummer, of een straat, "
        "huisnummer en plaats. Bevat geen persoonsgegevens en geen WOZ-waarde."
    ),
    annotations=READ_ONLY,
)
async def huis_info(
    huisnummer: Annotated[int, Field(ge=1, description="Huisnummer zonder letter of toevoeging")],
    postcode: Annotated[str | None, Field(description="Postcode, bijvoorbeeld 2311EZ")] = None,
    straat: Annotated[str | None, Field(description="Straatnaam, bijvoorbeeld Rapenburg. Alleen nodig zonder postcode")] = None,
    plaats: Annotated[str | None, Field(description="Woonplaats, bijvoorbeeld Leiden. Alleen nodig zonder postcode")] = None,
    toevoeging: Annotated[str | None, Field(description="Huisletter of toevoeging, bijvoorbeeld A of 2")] = None,
) -> dict[str, Any]:
    return await huis.huis_info(huisnummer, postcode, straat, plaats, toevoeging)


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
        "dichtstbijzijnde meetpunten met afstand en meettijd."
    ),
    annotations=READ_ONLY,
)
async def water_actueel(
    plaats: Annotated[str, Field(description="Plaats of meetpunt, bijvoorbeeld Scheveningen, Hoek van Holland of Lobith")],
    soort: Annotated[Literal["waterstand", "watertemperatuur", "beide"], LOWER, Field(description="Wat je wilt weten")] = "beide",
) -> dict[str, Any]:
    return await water.water_actueel(plaats, soort)


@server.tool(
    title="Treinvertrek",
    description=(
        "Vertrektijden van treinen vanaf een station, met vertraging, spoor, spoorwijzigingen en uitval. "
        "Zonder tijd krijg je de vertrekken vanaf nu. Gaat ongeveer een uur vooruit; gebruik voor later "
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
        "een uur vooruit; gebruik voor later Treinreis plannen. Rijdt een trein door, dan heeft hij bij "
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
        "prijs. Zonder datum en tijd wordt er gezocht vanaf nu."
    ),
    annotations=READ_ONLY,
)
async def trein_reis(
    van: Annotated[str, Field(description="Vertrekstation, bijvoorbeeld Den Haag Centraal")],
    naar: Annotated[str, Field(description="Aankomststation, bijvoorbeeld Utrecht Centraal")],
    datum: Annotated[str | None, Field(description="Datum als JJJJ-MM-DD. Leeg = vandaag")] = None,
    tijd: Annotated[str | None, Field(description="Tijd als UU:MM. Leeg = nu")] = None,
    aankomst: Annotated[bool, Field(description="True als de tijd de gewenste aankomsttijd is")] = False,
) -> dict[str, Any]:
    return await ns.trein_reis(van, naar, datum, tijd, aankomst)


@server.tool(
    title="Vluchtstatus Schiphol",
    description=(
        "Status van een vlucht van of naar Schiphol: geplande, verwachte en werkelijke tijd, gate, terminal, "
        "bagageband en status. Alleen vluchten via Schiphol."
    ),
    annotations=READ_ONLY,
)
async def vlucht_info(
    vluchtnummer: Annotated[str, Field(description="Vluchtnummer, bijvoorbeeld KL1001 of HV5131")],
    datum: Annotated[str | None, Field(description="Geplande datum als JJJJ-MM-DD. Leeg = vandaag")] = None,
) -> dict[str, Any]:
    return await schiphol.vlucht_info(vluchtnummer, datum)


@server.custom_route("/", methods=["GET"])
async def home(request: Request) -> PlainTextResponse:
    return PlainTextResponse("Nederland MCP draait. Koppel je AI-assistent aan /mcp.")

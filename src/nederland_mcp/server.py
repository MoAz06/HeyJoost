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

from . import __version__, vraag
from .bronnen import huis, ns, ov, vakanties, water

WEBSITE = "https://heyjoost.nl"
SITE = Path(__file__).parent / "site"

# Written as facts, not orders: directories reject servers that tell the assistant how to behave.
INSTRUCTIONS = (
    "Hey Joost geeft actuele Nederlandse open data uit officiële bronnen: adressen en gebouwen uit de BAG, "
    "schoolvakanties, waterstanden en watertemperatuur, NS-treinen, en bus, tram en metro.\n"
    "- Elk resultaat heeft een veld 'bron' met de herkomst van de data, voor de bronvermelding.\n"
    "- Alle tijden zijn Nederlandse tijd (Europe/Amsterdam).\n"
    "- De data bevat geen persoonsgegevens. De tools zijn niet bedoeld om mensen op te zoeken."
)

server = MCPServer(
    name="heyjoost",
    title="Hey Joost",
    description=(
        "Live Nederland in je AI: treinen, bus en tram, water, schoolvakanties en huizen, uit officiële bronnen."
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
    title="Bus, tram en metro",
    description=(
        "Vertrektijden van bus, tram, metro en veerboot vanaf een halte in Nederland, met live vertraging en "
        "uitval als de vervoerder die doorgeeft. Werkt met de naam van de halte en de plaats, bijvoorbeeld Spui "
        "in Den Haag of Leidseplein in Amsterdam, en ook met een adres, straat of postcode: dan komen de drie "
        "dichtstbijzijnde haltes mee in 'haltes_in_de_buurt', met de afstand hemelsbreed in meters ('afstand_m'). "
        "Bij een treinstation komen alle bus- en tramhaltes rond het station mee, elk met halte en perron. Zonder "
        "tijd krijg je de vertrekken vanaf nu; met datum en tijd ook later, volgens de dienstregeling. Met naar "
        "krijg je alleen de bussen en trams die daarna bij die halte stoppen, met de aankomsttijd daar "
        "('stopt_bij' en 'aankomst'), ook vanaf een adres, bijvoorbeeld naar het station. Een negatieve vertraging "
        "betekent te vroeg. 'andere_haltes' noemt haltes met een andere naam die ook gevonden zijn. Treinen staan "
        "in Treinvertrek."
    ),
    annotations=READ_ONLY,
)
async def ov_vertrek(
    halte: Annotated[str, Field(description="Naam van de halte, bijvoorbeeld Spui, Leidseplein of Leiden Centraal. Mag ook een adres, straat of postcode zijn, bijvoorbeeld Laan van Meerdervoort 100")],
    plaats: Annotated[str | None, Field(description="Plaats of wijk van de halte of het adres, bijvoorbeeld Den Haag of Scheveningen")] = None,
    aantal: Annotated[int, Field(ge=1, le=25, description="Aantal vertrekken")] = 10,
    lijn: Annotated[str | None, Field(description="Alleen deze lijn of soort, bijvoorbeeld 2, tram 2, 400, E of metro")] = None,
    naar: Annotated[str | None, Field(description="Alleen vertrekken die daarna bij deze halte stoppen, bijvoorbeeld Leiden Centraal of Kurhaus")] = None,
    datum: Annotated[str | None, Field(description="Datum als JJJJ-MM-DD. Leeg = vandaag")] = None,
    tijd: Annotated[str | None, Field(description="Vanaf welke tijd, als UU:MM. Leeg = nu")] = None,
) -> dict[str, Any]:
    return await ov.ov_vertrek(halte, plaats, aantal, lijn, datum, tijd, naar)


# The home page shows the MCP URL of the host it was reached on, so a local run shows its own address.
HOST = re.compile(r"(?P<name>[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(?::\d{1,5})?")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
# The version in the stylesheet link makes browsers load a changed stylesheet right away.
HOME = (SITE / "index.html").read_text(encoding="utf-8").replace("{{VERSION}}", __version__)


def _mcp_url(request: Request) -> str:
    match = HOST.fullmatch(request.headers.get("host", ""))
    if not match:
        return f"{WEBSITE}/mcp"
    scheme = "http" if match["name"].lower() in LOCAL_HOSTS else "https"
    return f"{scheme}://{match[0]}/mcp"


@server.custom_route("/", methods=["GET"])
async def home(request: Request) -> HTMLResponse:
    page = HOME.replace("{{MCP_URL}}", html.escape(_mcp_url(request)))
    return HTMLResponse(page.replace("{{VRAAG}}", "aan" if vraag.available() else "uit"))


@server.custom_route("/api/vraag", methods=["POST"])
async def ask_joost(request: Request) -> Response:
    return await vraag.ask(request, server)


def _serve(path: str, filename: str, media_type: str, max_age: int) -> None:
    body = (SITE / filename).read_bytes()
    if media_type == "text/html":
        body = body.replace(b"{{VERSION}}", __version__.encode())
    headers = {"Cache-Control": f"public, max-age={max_age}"}

    async def page(request: Request) -> Response:
        return Response(body, media_type=media_type, headers=headers)

    server.custom_route(path, methods=["GET"], name=filename)(page)


_serve("/privacy", "privacy.html", "text/html", 300)
_serve("/voorwaarden", "voorwaarden.html", "text/html", 300)
_serve("/style.css", "style.css", "text/css", 300)
_serve("/favicon.svg", "favicon.svg", "image/svg+xml", 86400)
# A font file never changes under the same name, so browsers may keep it for a year.
_serve("/fonts/plex-sans.woff2", "fonts/plex-sans.woff2", "font/woff2", 31536000)
_serve("/fonts/plex-mono-400.woff2", "fonts/plex-mono-400.woff2", "font/woff2", 31536000)
_serve("/fonts/plex-mono-600.woff2", "fonts/plex-mono-600.woff2", "font/woff2", 31536000)
_serve("/robots.txt", "robots.txt", "text/plain", 86400)

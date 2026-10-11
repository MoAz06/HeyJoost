# Nederland MCP

Actuele Nederlandse open data voor je AI-assistent (Claude, ChatGPT en andere MCP-clients). Vraag gewoon "hoe hoog staat het water in Scheveningen?" of "wanneer is de herfstvakantie in regio zuid?" en je assistent haalt het antwoord live bij de officiële bron.

## Wat kan het

| Tool | Wat je krijgt | Bron | Key nodig |
|---|---|---|---|
| `huis_info` | Bouwjaar, oppervlakte, gebruiksdoel, wijk en buurt van een adres, plus het energielabel. Met postcode of met straat en plaats | BAG (Kadaster) via PDOK, EP-online (RVO) | Alleen voor het energielabel |
| `schoolvakanties` | Schoolvakanties per regio en schooljaar | Rijksoverheid open data | Nee |
| `water_actueel` | Actuele waterstand ten opzichte van NAP en watertemperatuur bij een plaats | Rijkswaterstaat Waterinfo | Nee |
| `trein_vertrek` | Vertrektijden met vertraging, spoor en uitval, tot ongeveer een uur vooruit | NS Reisinformatie API | Ja (NS) |
| `trein_aankomst` | Aankomsttijden: waar de trein vandaan komt, vertraging en spoor. Handig als je iemand ophaalt | NS Reisinformatie API | Ja (NS) |
| `trein_reis` | Reisadvies met overstappen, drukte en prijs | NS Reisinformatie API | Ja (NS) |
| `vlucht_info` | Status, gate en bagageband van een vlucht via Schiphol | Schiphol Public Flight API | Ja (Schiphol) |

Elk antwoord heeft een veld `bron`, zodat je assistent altijd kan zeggen waar het vandaan komt. Er zitten geen persoonsgegevens in, en je kunt er geen mensen mee opzoeken.

## Lokaal draaien

Je hebt [uv](https://docs.astral.sh/uv/) nodig.

```bash
uv sync
uv run python scripts/smoke_test.py
```

De smoke test roept elke tool aan tegen de echte bronnen. Tools waarvoor je nog geen key hebt, worden overgeslagen.

### In Claude Desktop

Zet dit in `claude_desktop_config.json` en pas het pad aan:

```json
{
  "mcpServers": {
    "nederland": {
      "command": "uv",
      "args": ["--directory", "/pad/naar/nederland-mcp", "run", "nederland-mcp"]
    }
  }
}
```

### In Claude Code

```bash
claude mcp add nederland -- uv --directory /pad/naar/nederland-mcp run nederland-mcp
```

### Als webserver (hosting)

```bash
uv run nederland-mcp --http --host 0.0.0.0 --port 8000
```

De MCP-url is dan `http://<jouw-host>:8000/mcp`. Mensen voegen hem toe als remote MCP server of custom connector met die url, ze hoeven zelf niks te installeren. De server onthoudt niks tussen aanvragen (stateless), dus je kunt er gerust meerdere naast elkaar draaien. Op `/` staat een korte tekst, handig als health check.

Voor hosting staan er een `Dockerfile` en een `railway.json` klaar. Op Railway koppel je de GitHub-repo, kies je regio EU West (Amsterdam) en zet je de keys bij Variables. De poort komt vanzelf uit `$PORT`.

## API-keys

Kopieer `.env.example` naar `.env` en vul in wat je hebt. Tools zonder key geven een nette melding, de rest werkt gewoon.

- `NS_API_KEY`: gratis via het [NS API-portal](https://apiportal.ns.nl/product#product=NsApp), product NsApp. Gratis gebruikers mogen 300 aanvragen per 5 minuten doen. Op een publieke server delen alle gebruikers die limiet, de cache vangt veel op.
- `SCHIPHOL_CLIENT_ID` en `SCHIPHOL_CLIENT_SECRET`: maak een Application aan op [developer.schiphol.nl](https://developer.schiphol.nl). De secret zie je maar één keer. De server ruilt ze zelf om voor een token dat 30 minuten geldig is.
- `EP_ONLINE_API_KEY`: aanvragen via [ep-online.nl](https://www.ep-online.nl). Je hebt een KvK-nummer nodig.

## Bronnen en voorwaarden

| Bron | Voorwaarden |
|---|---|
| BAG en Locatieserver via PDOK | Open data, publiek domein |
| Rijksoverheid schoolvakanties | CC0 |
| Rijkswaterstaat Waterinfo | Open data. Actuele metingen zijn ongecontroleerd. Licentie nog nalopen voor publieke lancering |
| NS Reisinformatie API | Het product Ns-App heeft geen eigen gebruiksvoorwaarden. Gratis gebruikers mogen 300 aanvragen per 5 minuten doen. Voor meer, of voor betaald gebruik, contact opnemen met NS |
| Schiphol Public Flight API | [Voorwaarden](https://developer.schiphol.nl/terms-and-conditions) (december 2025): alleen om reizigers en ophalers te helpen, realtime, maximaal 24 uur cachen, niet in bulk doorgeven of doorverkopen. Commercieel gebruik alleen met toestemming van Schiphol. Keys elke 90 dagen vernieuwen |
| EP-online | Voorwaarden RVO, key hoort bij een KvK-nummer |

## Hoe het in elkaar zit

- `src/nederland_mcp/server.py`: de tools en hun beschrijvingen.
- `src/nederland_mcp/bronnen/`: één bestand per bron.
- `src/nederland_mcp/common.py`: HTTP-client, cache en tijdzone (alles in Nederlandse tijd).
- Alle aanvragen gaan via een cache, zodat de bronnen niet onnodig belast worden: stations, adressen en vakanties een dag, water 5 minuten, treinen en vluchten 30 tot 60 seconden.
- Foutmeldingen zijn in het Nederlands en vertellen wat je kunt doen.
- Wat gebruikers opvragen (adressen, plaatsen) komt niet in de logs.

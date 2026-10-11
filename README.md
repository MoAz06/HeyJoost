# Hey Joost

Actuele Nederlandse open data voor je AI-assistent (Claude, ChatGPT en andere MCP-clients). Te gebruiken via [heyjoost.nl](https://heyjoost.nl): voeg `https://heyjoost.nl/mcp` toe als connector, zonder installeren. Vraag gewoon "hoe hoog staat het water in Scheveningen?" of "wanneer is de herfstvakantie in regio zuid?" en je assistent haalt het antwoord live bij de officiële bron. Op de website kun je Joost ook direct iets vragen.

## Wat kan het

| Tool | Wat je krijgt | Bron | Key nodig |
|---|---|---|---|
| `huis_info` | Bouwjaar, oppervlakte, gebruiksdoel, wijk en buurt van een adres, plus het energielabel. Met postcode of met straat en plaats | BAG (Kadaster) via PDOK, EP-online (RVO) | Alleen voor het energielabel |
| `schoolvakanties` | Schoolvakanties per regio en schooljaar | Rijksoverheid open data | Nee |
| `water_actueel` | Actuele waterstand ten opzichte van NAP en watertemperatuur bij een plaats | Rijkswaterstaat Waterinfo | Nee |
| `trein_vertrek` | Vertrektijden met vertraging, spoor en uitval, tot ongeveer een uur vooruit | NS Reisinformatie API | Ja (NS) |
| `trein_aankomst` | Aankomsttijden: waar de trein vandaan komt, vertraging en spoor. Handig als je iemand ophaalt | NS Reisinformatie API | Ja (NS) |
| `trein_reis` | Reisadvies met overstappen, drukte en prijs | NS Reisinformatie API | Ja (NS) |
| `ov_vertrek` | Vertrektijden van bus, tram, metro en veerboot vanaf een halte, met live vertraging en uitval. Bij een treinstation alle haltes eromheen. Vanaf een adres of straat de drie dichtstbijzijnde haltes, met de afstand. Met `naar` alleen de ritten die bij een bepaalde halte stoppen, met de aankomsttijd daar | Data van de vervoerders via OpenOV, opgehaald via Transitous | Nee |
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
    "heyjoost": {
      "command": "uv",
      "args": ["--directory", "/pad/naar/HeyJoost", "run", "nederland-mcp"]
    }
  }
}
```

### In Claude Code

```bash
claude mcp add heyjoost -- uv --directory /pad/naar/HeyJoost run nederland-mcp
```

### Als webserver (hosting)

```bash
uv run nederland-mcp --http --host 0.0.0.0 --port 8000
```

De MCP-url is dan `http://<jouw-host>:8000/mcp`. Mensen voegen hem toe als remote MCP server of custom connector met die url, ze hoeven zelf niks te installeren. De server onthoudt niks tussen aanvragen (stateless), dus je kunt er gerust meerdere naast elkaar draaien. Op `/` staat de website met uitleg, privacy en voorwaarden (bestanden in `src/nederland_mcp/site/`), ook handig als health check.

Voor hosting staan er een `Dockerfile` en een `railway.json` klaar. Op Railway koppel je de GitHub-repo, kies je regio EU West (Amsterdam) en zet je de keys bij Variables. De poort komt vanzelf uit `$PORT`.

## API-keys

Kopieer `.env.example` naar `.env` en vul in wat je hebt. Tools zonder key geven een nette melding, de rest werkt gewoon.

- `NS_API_KEY`: gratis via het [NS API-portal](https://apiportal.ns.nl/product#product=NsApp), product NsApp. Gratis gebruikers mogen 300 aanvragen per 5 minuten doen. Op een publieke server delen alle gebruikers die limiet, de cache vangt veel op.
- `SCHIPHOL_CLIENT_ID` en `SCHIPHOL_CLIENT_SECRET`: maak een Application aan op [developer.schiphol.nl](https://developer.schiphol.nl). De secret zie je maar één keer. De server ruilt ze zelf om voor een token dat 30 minuten geldig is.
- `EP_ONLINE_API_KEY`: aanvragen via [ep-online.nl](https://www.ep-online.nl). Je hebt een KvK-nummer nodig.
- `ANTHROPIC_API_KEY`: voor Vraag het Joost, de chat op de website. Maak een key aan op [platform.claude.com](https://platform.claude.com). Zonder key staat de chat uit en werkt de rest gewoon. De chat gebruikt Claude Haiku 5.5 en kost ongeveer een tiende cent per vraag. `VRAAG_DAGBUDGET_USD` (standaard 0.50) is het maximum per dag in dollars, `VRAAG_PER_DAG` (standaard 30) het aantal vragen per bezoeker per dag. Is het dagbudget op, dan verdwijnt de chat tot de volgende dag.

## Bronnen en voorwaarden

| Bron | Voorwaarden |
|---|---|
| BAG en Locatieserver via PDOK | Open data, publiek domein |
| Rijksoverheid schoolvakanties | CC0 |
| Rijkswaterstaat Waterinfo | Publiek domein volgens data.overheid.nl (dataset Waterinfo Rijkswateren). Actuele metingen zijn ongecontroleerd |
| NS Reisinformatie API | Het product Ns-App heeft geen eigen gebruiksvoorwaarden. Gratis gebruikers mogen 300 aanvragen per 5 minuten doen. Voor meer, of voor betaald gebruik, contact opnemen met NS |
| Schiphol Public Flight API | [Voorwaarden](https://developer.schiphol.nl/terms-and-conditions) (december 2025): alleen om reizigers en ophalers te helpen, realtime, maximaal 24 uur cachen, niet in bulk doorgeven of doorverkopen. Commercieel gebruik alleen met toestemming van Schiphol. Keys elke 90 dagen vernieuwen |
| EP-online | Voorwaarden RVO, key hoort bij een KvK-nummer |
| Transitous (bus, tram, metro) | [Gebruiksbeleid](https://transitous.org/api/): alleen voor open-source projecten, niet commercieel. User-Agent met naam, versie en contact, en een zichtbare link naar [transitous.org/sources](https://transitous.org/sources/) plus de OpenStreetMap-vermelding. Bij veel gebruikers of voor routeplanning eerst contact opnemen via hun Matrix-kanaal. Geen garantie op beschikbaarheid |

## Hoe het in elkaar zit

- `src/nederland_mcp/server.py`: de tools en hun beschrijvingen, plus de routes van de website.
- `src/nederland_mcp/site/`: de website (startpagina, privacy, voorwaarden).
- `src/nederland_mcp/bronnen/`: één bestand per bron.
- `src/nederland_mcp/common.py`: HTTP-client, cache en tijdzone (alles in Nederlandse tijd).
- `src/nederland_mcp/vraag.py`: de chat op de website. Claude krijgt dezelfde tools als via MCP, en de server onthoudt geen gesprekken.
- Alle aanvragen gaan via een cache, zodat de bronnen niet onnodig belast worden: stations, haltes, adressen en vakanties een dag, water 5 minuten, treinen, bussen en vluchten 30 tot 60 seconden.
- Foutmeldingen zijn in het Nederlands en vertellen wat je kunt doen.
- Wat gebruikers opvragen (adressen, plaatsen) komt niet in de logs.

## Licentie

De code valt onder de [MIT-licentie](LICENSE). De data blijft van de bronnen hierboven, met hun eigen voorwaarden. De naam Hey Joost en het logo vallen niet onder de licentie.

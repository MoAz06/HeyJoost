# Hey Joost

Actuele Nederlandse open data voor je AI-assistent (Claude, ChatGPT en andere MCP-clients). Te gebruiken via [heyjoost.nl](https://heyjoost.nl): voeg `https://heyjoost.nl/mcp` toe als connector, zonder installeren. Vraag gewoon "wordt het morgen droog in Utrecht?" of "wanneer is de herfstvakantie in regio zuid?" en je assistent haalt het antwoord live bij de officiële bron. Op de website kun je Joost ook direct iets vragen.

## Wat kan het

| Tool | Wat je krijgt | Bron | Key nodig |
|---|---|---|---|
| `huis_info` | Bouwjaar, oppervlakte, gebruiksdoel, wijk en buurt van een adres, plus het energielabel. Met postcode of met straat en plaats | BAG (Kadaster) via PDOK, EP-online (RVO) | Alleen voor het energielabel |
| `buurt_info` | Cijfers over een buurt, wijk of gemeente: inwoners en leeftijden, huishoudens, woningen met de gemiddelde WOZ-waarde en koop of huur, inkomen, energieverbruik en zonnepanelen, afstand tot huisarts, supermarkt en school, en auto's. Per onderdeel het jaar, met de gemeente en Nederland ernaast. Met een buurt, wijk, plaats, straat met plaats of postcode. Een gebied dat het CBS niet als één geheel telt, zoals de Bijlmer, Haren of Kralingen, geeft de wijken of buurten naast elkaar | CBS Kerncijfers wijken en buurten, plaatsen via PDOK | Nee |
| `schoolvakanties` | Schoolvakanties per regio en schooljaar | Rijksoverheid open data | Nee |
| `water_actueel` | Actuele waterstand ten opzichte van NAP en watertemperatuur bij een plaats | Rijkswaterstaat Waterinfo | Nee |
| `water_verwachting` | Eb en vloed: hoog- en laagwater voor vandaag en morgen of een dag tot een jaar vooruit. Voor de komende twee dagen met de verwachting van Rijkswaterstaat (wind en rivieren meegerekend), met de getijtabel ernaast. Aan rivieren en meren zonder getij de verwachte waterstand | Rijkswaterstaat Waterinfo (WaterWebservices) | Nee |
| `weer` | Het weer nu, per uur voor de komende 24 uur en per dag voor de week: temperatuur, neerslag, wind in Beaufort en windstoten. Met een datum die dag per uur. Voor een plaats, wijk, station of postcode. Geldt er een KNMI-waarschuwing voor die plek, dan staat die erbij | KNMI (HARMONIE-AROME) en ECMWF via Open-Meteo, waarschuwingen van het KNMI | Nee (waarschuwingen: ja, KNMI) |
| `regen` | Regen voor de komende twee uur per tien minuten, uit de neerslagradar, met een samenvatting zoals "droog tot 14:20, dan lichte regen" | KNMI Data Platform (WMS) | Ja (KNMI) |
| `weerwaarschuwingen` | Code geel, oranje of rood: wat, waar, wanneer en wat je moet doen. Voor een plaats, een provincie of heel Nederland | KNMI Data Platform (Open Data API) | Ja (KNMI) |
| `stroomprijs` | De stroomprijs per kwartier voor nu, vandaag en morgen, of een dag tot een maand terug: kaal en met energiebelasting en btw, in cent per kWh, met de goedkoopste momenten (kwartier, uur, drie uur) | Bundesnetzagentur (SMARD.de) via Energy-Charts, met SMARD zelf als reserve | Nee |
| `trein_vertrek` | Vertrektijden met vertraging, spoor en uitval, tot ongeveer een uur vooruit | NS Reisinformatie API | Ja (NS) |
| `trein_aankomst` | Aankomsttijden: waar de trein vandaan komt, vertraging en spoor. Handig als je iemand ophaalt | NS Reisinformatie API | Ja (NS) |
| `trein_reis` | Reisadvies met overstappen, drukte en prijs. Rijdt er een bus in plaats van de trein, dan staat het bericht van NS erbij | NS Reisinformatie API | Ja (NS) |
| `trein_storingen` | Storingen en werkzaamheden op het spoor: wat, waar, wanneer, de bussen die dan rijden en het advies van NS. Nu en de komende drie dagen, of een periode tot 31 dagen. Voor heel Nederland of het spoor langs één station | NS Reisinformatie API | Ja (NS) |
| `ov_vertrek` | Vertrektijden van bus, tram, metro en veerboot vanaf een halte, met live vertraging en uitval. Bij een treinstation alle haltes eromheen. Vanaf een adres of straat de drie dichtstbijzijnde haltes, met de afstand. Met `naar` alleen de ritten die bij een bepaalde halte stoppen, met de aankomsttijd daar. Bij een staking in het streekvervoer het bericht van NS erbij | Data van de vervoerders via OpenOV, opgehaald via Transitous. Stakingsberichten van NS | Nee |

Elk antwoord heeft een veld `bron`, zodat je assistent altijd kan zeggen waar het vandaan komt. Er zitten geen persoonsgegevens in, en je kunt er geen mensen mee opzoeken.

In de chat op de website zoekt Joost ook vluchten van en naar Schiphol op (`vlucht_info`). Dat zit expres niet in de MCP-server: delen via MCP valt volgens Schiphol niet onder hun voorwaarden, de chat op je eigen website wel. Daarom gelden er grenzen: één vlucht per keer, alleen gisteren, vandaag en morgen, geen lijsten en geen geschiedenis, een limiet per bezoeker en voor de hele site, en Schiphol staat als bron bij elk antwoord.

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
- `EP_ONLINE_API_KEY`: aanvragen via [ep-online.nl](https://www.ep-online.nl). Je hebt een KvK-nummer nodig.
- `KNMI_OPEN_DATA_API_KEY` en `KNMI_WMS_API_KEY`: gratis via het [KNMI Data Platform](https://developer.dataplatform.knmi.nl/), na inloggen bij Open Data API en bij WMS API. Eén account per persoon, elke API heeft zijn eigen key. De Open Data-key is voor de weerwaarschuwingen, de WMS-key voor de regenradar.
- `ANTHROPIC_API_KEY`: voor Vraag het Joost, de chat op de website. Maak een key aan op [platform.claude.com](https://platform.claude.com). Zonder key staat de chat uit en werkt de rest gewoon. De chat gebruikt Claude Haiku 5.5 en kost ongeveer een tiende cent per vraag. `VRAAG_DAGBUDGET_USD` (standaard 0.50) is het maximum per dag in dollars, `VRAAG_PER_DAG` (standaard 30) het aantal vragen per bezoeker per dag. Is het dagbudget op, dan verdwijnt de chat tot de volgende dag.
- `SCHIPHOL_CLIENT_ID` en `SCHIPHOL_CLIENT_SECRET`: voor vluchten in de chat, via een app op [developer.schiphol.nl](https://developer.schiphol.nl). Zonder deze keys noemt de site geen vluchten. `VLUCHT_PER_DAG` (standaard 10) is het aantal vluchten per bezoeker per dag, `VLUCHT_SITE_PER_DAG` (standaard 300) het maximum voor de hele site.

## Bronnen en voorwaarden

| Bron | Voorwaarden |
|---|---|
| BAG en Locatieserver via PDOK | Open data, publiek domein |
| Rijksoverheid schoolvakanties | CC0 |
| Rijkswaterstaat Waterinfo | Publiek domein volgens data.overheid.nl (dataset Waterinfo Rijkswateren), en CC0 volgens [rijkswaterstaatdata.nl](https://rijkswaterstaatdata.nl/waterdata/). Actuele metingen zijn ongecontroleerd. Het getij (astronomisch) staat er tot ongeveer een jaar vooruit in, de verwachting met wind ongeveer twee dagen. Niet elk meetpunt in de catalogus heeft een getijtabel, dus de server probeert de drie dichtstbijzijnde |
| NS Reisinformatie API | Het product Ns-App heeft geen eigen gebruiksvoorwaarden. Gratis gebruikers mogen 300 aanvragen per 5 minuten doen. Voor meer, of voor betaald gebruik, contact opnemen met NS |
| EP-online | Voorwaarden RVO, key hoort bij een KvK-nummer |
| Schiphol Public Flight API | Alleen in de chat op je eigen website, niet via MCP. Eén vlucht per keer, alleen gisteren tot en met morgen, een limiet per bezoeker en per dag, Schiphol als bron bij elk antwoord en hooguit een minuut in de cache |
| KNMI Data Platform (regenradar, weerwaarschuwingen) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), gratis, ook commercieel ([fair-use-beleid](https://developer.dataplatform.knmi.nl/fair-use)): noem het KNMI als bron en wek niet de indruk dat het KNMI Hey Joost steunt. Keys zijn persoonlijk. De WMS-key mag 1.000 aanvragen per uur en 20 per seconde doen, de Open Data-key 1.000 per uur. De regenradar vraagt elk tijdstip apart op, omdat de server bij meerdere tijdstippen tegelijk alleen het eerste geeft. Het KNMI vervangt de waarschuwingen op 2 november 2026 door een nieuwe dataset met gebieden op de kaart; de server gebruikt die vanzelf zodra hij echt is, en tot dan de oude per provincie |
| Open-Meteo (weer) | Gratis voor niet-commercieel gebruik, tot 10.000 aanvragen per dag. De data valt onder [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): noem Open-Meteo als bron, met een link naar [open-meteo.com](https://open-meteo.com/) bij de data. Voor commercieel gebruik is een abonnement nodig |
| SMARD.de via Energy-Charts (stroomprijs) | De Bundesnetzagentur geeft de day-ahead-prijzen, ook die van Nederland, onder [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): noem "Bundesnetzagentur \| SMARD.de". Energy-Charts (Fraunhofer ISE) mag 2 aanvragen per minuut per server; daarboven vraagt de server het rechtstreeks aan SMARD. De prijzen van morgen komen na de veiling van 12:00. De energiebelasting staat per jaar in `bronnen/stroom.py` (`TAX`): 2026 van de Belastingdienst, 2027 uit het Belastingplan 2027. Controleer die in december, als het Belastingplan is aangenomen |
| CBS Kerncijfers wijken en buurten | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): noem het CBS als bron. Elk jaar een nieuwe tabel (2026: 86358NED, 2025: 86165NED, 2024: 85984NED), die het CBS in stappen vult. Een tabel die het CBS vernieuwt, is soms een paar uur dicht |
| Transitous (bus, tram, metro) | [Gebruiksbeleid](https://transitous.org/api/): alleen voor open-source projecten, niet commercieel. User-Agent met naam, versie en contact, en een zichtbare link naar [transitous.org/sources](https://transitous.org/sources/) plus de OpenStreetMap-vermelding. Bij veel gebruikers of voor routeplanning eerst contact opnemen via hun Matrix-kanaal. Geen garantie op beschikbaarheid |

## Hoe het in elkaar zit

- `src/nederland_mcp/server.py`: de tools en hun beschrijvingen, plus de routes van de website.
- `src/nederland_mcp/site/`: de website (startpagina, privacy, voorwaarden), met de lettertypes zelf erbij, zodat de site niks van anderen laadt. Je kunt Joost op je beginscherm zetten (manifest en iconen), vragen bewaren (alleen in de browser) en een antwoord delen met een link die de vraag na de `#` meegeeft, zodat de server hem niet ziet.
- `src/nederland_mcp/bronnen/`: één bestand per bron.
- `src/nederland_mcp/common.py`: HTTP-client, cache en tijdzone (alles in Nederlandse tijd).
- `src/nederland_mcp/vraag.py`: de chat op de website. Claude krijgt dezelfde tools als via MCP, plus vluchten van Schiphol, en de server onthoudt geen gesprekken.
- `src/nederland_mcp/tellers.py`: anonieme tellingen per dag, zoals hoe vaak de site, de chat en elke tool gebruikt worden. Ze staan elk uur als één regel in de log: zoek op `Tellers`. Wie iets vroeg of wat, staat er niet in. Kan de chat een vraag niet beantwoorden, dan telt hij alleen het onderwerp, uit een vaste lijst (`MISSED` in `vraag.py`), als `chat mist <onderwerp>`: zo zie je wat mensen nog missen.
- Alle aanvragen gaan via een cache, zodat de bronnen niet onnodig belast worden: buurtcijfers een week, stations, haltes, adressen, plaatsen, vakanties en oudere stroomprijzen een dag, het getij 12 uur, de waterverwachting een half uur, het weer, de stroomprijs en werkzaamheden op het spoor 15 minuten, de regenradar 10 minuten, water en weerwaarschuwingen 5 minuten, storingen op het spoor 2 minuten, treinen, bussen en vluchten 30 tot 60 seconden.
- Foutmeldingen zijn in het Nederlands en vertellen wat je kunt doen.
- Wat gebruikers opvragen (adressen, plaatsen) komt niet in de logs.

## Licentie

De code valt onder de [MIT-licentie](LICENSE). De data blijft van de bronnen hierboven, met hun eigen voorwaarden. De naam Hey Joost en het logo vallen niet onder de licentie. De lettertypes IBM Plex Sans en IBM Plex Mono vallen onder de SIL Open Font License 1.1, zie `src/nederland_mcp/site/fonts/`.

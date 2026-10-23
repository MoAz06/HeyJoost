# Testvragen over reizen

20 vragen voor de chat. Elke vraag gaat 3 keer, want Haiku antwoordt niet elke keer hetzelfde.

| # | Vraag | Wat goed is | Soort |
|---|---|---|---|
| 1 | Hoe laat gaat de volgende trein van Leiden naar Utrecht? | Zoekt nu vanaf Leiden Centraal naar Utrecht Centraal en raadt de beste reis aan. | vertrek, nu |
| 2 | Ik sta op Den Haag Centraal en moet naar Schiphol. Welke trein neem ik? | Zoekt nu en raadt de beste reis aan; een directe trein mag als alternatief. | vertrek, nu |
| 3 | Volgende trein van Breda naar Amsterdam? | Raadt de beste reis aan en noemt de toeslag als die met de Intercity direct gaat. | vertrek, nu, toeslag |
| 4 | Morgen om 8 uur wil ik van Den Haag HS naar Eindhoven. Welke trein? | Zoekt morgen vanaf 08:00 en raadt de beste reis aan. | vertrek, dag |
| 5 | Wat is maandag rond 15:40 de beste trein van Rotterdam naar Amsterdam Centraal? | Raadt de reis aan die het vroegst aankomt (vaak de Intercity direct), niet de kortste, en noemt de toeslag. | vertrek, dag, toeslag |
| 6 | Maandag om 15:40 van Haarlem naar Den Haag Centraal, welke trein? | Raadt de beste reis aan; noemt bij een overstap de juiste minuten. | vertrek, dag, overstap |
| 7 | Ik wil morgenavond om 7 uur met de trein van Amsterdam naar Groningen vertrekken. Welke trein neem ik? | Zoekt morgen vanaf 19:00, raadt de beste reis aan en noemt de overstap met de juiste minuten. | vertrek, dag, overstap |
| 8 | Ik ga morgenochtend om half 9 van Leiden naar Eindhoven. Moet ik overstappen? | Zoekt morgen vanaf 08:30 en zegt of en waar je overstapt, met de juiste minuten. | vertrek, dag, overstap |
| 9 | Wanneer gaat de volgende trein van Voorschoten naar Delft? | Vindt de kleine stations en raadt de beste reis aan. | vertrek, nu, klein station |
| 10 | When is the next train from Amsterdam Centraal to Schiphol Airport? | Antwoordt in het Engels en raadt de beste reis aan. | vertrek, nu, engels |
| 11 | Wat is vanavond de laatste trein van Utrecht naar Zwolle? | Zoekt laat op de avond en noemt de laatste trein van de avond, niet de eerste die het vindt. | vertrek, laatste, moeilijk |
| 12 | Wat is morgenochtend de eerste trein van Zwolle naar Groningen? | Zoekt morgen vroeg en noemt de eerste trein van de ochtend. | vertrek, eerste, dag, moeilijk |
| 13 | Ik moet maandag om 9 uur op Amsterdam Centraal zijn, ik kom uit Rotterdam. Welke trein? | Zoekt op aankomst 08:55: geen looptijd want het is op het station zelf, wel 5 minuten marge. | aankomst, dag, op het station |
| 14 | Hoe kom ik maandag om 9 uur in Amsterdam als ik uit Rotterdam kom? | Zoekt op aankomst tussen 08:40 en 08:55: het adres is onbekend, dus wat looptijd mag, plus 5 minuten marge. | aankomst, dag |
| 15 | Ik heb maandag om 10:00 een afspraak bij het Erasmus MC en ik kom uit Leiden. Welke trein moet ik nemen? | Zoekt op aankomst in Rotterdam tussen 09:20 en 09:50: het laatste stuk (lopen of tram) plus 5 minuten marge. | aankomst, dag, laatste stuk |
| 16 | Ik moet morgen om half 9 op Den Haag Centraal zijn, ik kom uit Delft. | Zoekt op aankomst morgen 08:25: op het station zelf, dus alleen 5 minuten marge. | aankomst, dag, op het station |
| 17 | Mijn vlucht gaat zaterdag om 10:00 vanaf Schiphol en ik kom uit Den Haag. Welke trein moet ik nemen? | Zoekt op aankomst ruim voor de vlucht (uiterlijk 08:30), niet om 10:00. | aankomst, dag, vlucht, moeilijk |
| 18 | Ik moet dinsdag om 9 uur op Utrecht Science Park zijn en ik woon in Gouda. Hoe kom ik daar? | Zoekt een trein naar Utrecht Centraal die op tijd is voor de tram of bus naar het Science Park; welke trein mag Joost zelf kiezen, passend bij die aansluiting. | aankomst, dag, laatste stuk, moeilijk |
| 19 | volgende trein amsterdm naar utreht | Snapt de stations ondanks de typfouten en raadt de beste reis aan. | robuust, nu |
| 20 | Ik ga zaterdag van Utrecht naar Zwolle, heen rond 10 uur en terug rond 17 uur. Welke treinen? | Zoekt twee keer, heen en terug, en noemt voor allebei een trein. | robuust, dag, twee reizen |

## Wat we bij elk antwoord nakijken

Alles automatisch, tegen de NS-gegevens die de chat bij dat antwoord kreeg:

1. **Zoekactie**: de juiste stations, vertrek of aankomst, de juiste dag en tijd.
2. **Beste reis eerst**: de reis die het eerst wordt aangeraden is de reis met 'beste' (bij 'laatste' en 'eerste' de laatste of eerste trein).
3. **Geen verzonnen tijden**: elke tijd in het antwoord staat in de NS-gegevens of in de vraag.
4. **Geen verzonnen sporen en prijzen**: elk spoor en elke prijs staat in de NS-gegevens.
5. **Overstap klopt**: de genoemde overstapminuten kloppen, en krap (onder 5), genoeg (5 tot 10) of ruim (10 of meer) past erbij.
6. **Toeslag**: genoemd als de aangeraden reis een toeslag heeft.
7. **Hooguit één andere reis** naast de aangeraden reis.
8. **Dag klopt**: geen 'morgen' of 'een week van tevoren' als dat niet zo is.
9. **Geen bekende fouten**, zoals 'stopt niet onderweg' (behalve als NS 0 tussenstops geeft).
10. **Taal**: een Engelse vraag krijgt een Engels antwoord.

Een antwoord is **goed** als alle punten die erop van toepassing zijn kloppen.

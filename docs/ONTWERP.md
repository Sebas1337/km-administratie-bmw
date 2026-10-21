# Ontwerp: automatische kilometeradministratie

Dit document beschrijft hoe kmlog werkt en waarom het zo is gebouwd: van de kilometerstand van de
auto (BMW CarData API) via ritten tot een maandstaat met te declareren kilometers. De sectienummers
zijn stabiel; de code verwijst ernaar (bijv. "ONTWERP §7.2").

Inhoud

1. Doel en context
2. Werking in het kort
3. Eisen en randvoorwaarden
4. Overwogen en afgewezen alternatieven
5. Architectuur (AWS)
6. BMW CarData API
7. Afleiding en regels (observaties → ritten), met voorbeelddagen
8. Datamodel
9. Uitvoer en bevestigingsflow
10. Meldgedrag en bekende beperkingen
11. Beslislog
12. Bronnen

---

## 1. Doel en context

Wie met een privé-auto zakelijke ritten rijdt voor de eigen BV, kan daarvoor een (onbelaste)
kilometervergoeding uit de BV ontvangen. Dat vraagt een kilometeradministratie, meestal een
handmatig bijgehouden spreadsheet. kmlog **automatiseert die registratie volledig**: elke maand
ontstaat zonder handwerk een maandstaat, en alleen bij uitzonderingen is één bevestiging nodig.

De uitvoer is bedoeld voor de eigenaar zelf: hoeveel kilometervergoeding de BV moet uitbetalen.
Het is geen "sluitende rittenregistratie" zoals bij bijtelling (dat regime geldt niet voor een
privé-auto met kilometervergoeding). Het tarief is per jaar instelbaar; controleer het jaarlijks.

Een ander vervoermiddel dat van de BV is (bijv. een motor) krijgt geen kilometervergoeding. Omdat de
auto de sensor is, vallen dagen waarop de auto niet rijdt vanzelf weg.

## 2. Werking in het kort

- Elk uur leest een Lambda de kilometerstand en positie van de auto via de officiële BMW CarData API.
- Opeenvolgende metingen met dezelfde stand zijn een **parkeer-episode**; tussen twee episodes ligt
  een **leg** met exacte kilometers (verschil in stand).
- Legs worden gematcht aan bekende **locaties** (thuis, klant, …) en **routes** met hun afstand.
  Past een rit bij een bekende route (±5 km), dan telt hij volledig zakelijk. Zo niet, dan is het
  zakelijke deel de **standaardroute** en de rest privé (conservatief), met één knop om te
  overrulen (bijv. omweg door file).
- Dagelijks een mail met nieuwe vragen; op de 1e van de maand de maandstaat (.xlsx) per mail.
- Een webinterface (inloggen met maillink) voor vragen, correcties, handmatige ritten en stamdata.

## 3. Eisen en randvoorwaarden

Functioneel

- F1. Registreer automatisch elke zakelijke autorit met datum, van/naar en **werkelijk gereden km**
  (routes variëren, bijv. via een andere snelweg bij file).
- F2. Dagen zonder autorit (bijv. met een ander vervoermiddel) leveren geen rit op.
- F3. Tussenstops en omwegen worden correct afgehandeld (§7).
- F4. Alleen bij uitzonderingen is een bevestiging nodig, met één tik.
- F5. Maandelijks automatisch een maandstaat (.xlsx) met ritten, km, tarief, bedrag en
  kilometerstand begin/einde maand; plus een jaaroverzicht.
- F6. Tarief per jaar instelbaar.

Randvoorwaarden

- R1. **Officiële BMW CarData API**, geen reverse-engineered client (`bimmer_connected`).
- R2. **Pollen hooguit 1× per uur.** Het quotum is 50 calls/24u; stops van uren (nacht, werkdag)
  zijn met uurpollen ruim zichtbaar.
- R3. **AWS**, serverless, kosten ≈ 0.
- R4. Geen afhankelijkheid van het thuisnetwerk, geen inbound verbinding.
- R5. Geheimen alleen in Secrets Manager (AWS); de AWS-deploycredentials in een lokale, gitignored `.env`.
- R6. Conservatief bij twijfel: een vergeten bevestiging mag nooit tot een te hoge declaratie leiden.

## 4. Overwogen en afgewezen alternatieven

| Alternatief | Waarom niet |
|---|---|
| Home Assistant als sensor-/zonelaag (BMW-integratie, zones, Companion-app) | Niet nodig: alles kan direct in de cloud; zou twee systemen opleveren i.p.v. één. |
| `bimmer_connected` (onofficiële My BMW-client) | Onofficieel, captcha bij login, afhankelijk van reverse engineering. De officiële CarData API bestaat sinds de EU Data Act (2025). |
| Telefoon-geofence (iOS Opdrachten "aankomst op locatie") als primaire bron | Kan auto en ander vervoer niet onderscheiden. Hooguit secundaire bron. |
| iOS Opdrachten op "CarPlay verbonden/verbroken" + locatie → API | Redelijk plan B: onderscheidt de auto, geeft begin/eindpunt per rit, maar geen kilometerstand. |
| Agenda als bron | Klantdagen staan niet per se in de agenda. |
| Rittenregistratie-apps (MileIQ, Driversnote, Everlance, TripLog) | Abonnement, locatiedata bij derde partij, vast aan hun export. |
| OBD/GPS-dongle | Overkill; sluitende registratie is niet vereist voor een privé-auto met kilometervergoeding. |
| Vaste afstanden per traject i.p.v. kilometerstand | Werkelijke km gewenst (omwegen door file). Vaste afstanden blijven wél de referentie voor route-matching en de conservatieve default. |
| MQTT-stream van CarData (push, telt niet mee voor quotum) | Vereist een altijd-aan proces. Niet nodig bij uurpollen. |

## 5. Architectuur (AWS)

```
EventBridge Scheduler (elk uur; de poller slaat uren buiten het venster over, standaard 06–23)
   └─► Lambda kmlog-poller
         ├─ Secrets Manager kmlog/cardata: client_id, roterende tokens, vin, container id
         ├─ OAuth refresh als het access token verlopen is; nieuwe refresh token direct opslaan
         ├─ quotumteller per UTC-dag in DynamoDB (harde stop op 40 van de 50 calls)
         ├─ GET telematicData → kilometerstand, lat/lon + tijdstempels → Observation
         └─ afleiding + classificatie (§7) van gisteren en vandaag, idempotent

EventBridge Scheduler (dagelijks 20:00)
   └─► Lambda kmlog-notify: herberekent de laatste 31 dagen, verloopt oude vragen,
         mailt nieuwe vragen (en desgewenst "gemist?"-dagen) als er iets nieuws is

EventBridge Scheduler (1e van de maand 06:00)
   └─► Lambda kmlog-report → maandstaat .xlsx (+ jaaroverzicht in december) → S3 → SES-mail

Lambda kmlog-web achter een Function URL: webinterface (§9)

Opslag: DynamoDB (on-demand, single-table), S3 (rapporten), Secrets Manager
Observability: CloudWatch-alarms (pollfouten, reportfouten, te weinig polls in 24 u) → SNS-mail;
               AWS Budget op tag project=kmlog
IaC: Terraform, state in S3 met S3-native locking
```

Alle rollen hebben een permissions boundary, zodat de deploy-gebruiker via een Lambda nooit meer
rechten kan krijgen dan bedoeld. Deploystappen: `README.md` en `infra/README.md`.

Lokaal ontwikkelen: dezelfde Python-package met een SQLite-store, zodat afleiding en rapportage
zonder AWS getest kunnen worden.

## 6. BMW CarData API

De officiële documentatie zit achter een login in het portaal
(`https://bmw-cardata.bmwgroup.com/customer/public/api-documentation/…`). Onderstaande is
geverifieerd tegen de broncode van drie community-clients (zie §12) en in de praktijk met een BMW i4.

- **Toegang**: in het My BMW-portaal (Voertuigenoverzicht → "BMW CarData") een
  *CarData Client* aanmaken → Client ID (account-breed). Abonneer de client op "CarData API"
  **vóór** de login, anders mist het token de scope (403 `CU-103`). Scopes soms één voor één
  toevoegen met ~30 s ertussen.
- **Device-code flow**: `POST https://customer.bmwgroup.com/gcdm/oauth/device/code` met `client_id`,
  `scope`, `response_type=device_code` en **PKCE** (`code_challenge`, `code_challenge_method=S256`);
  token: `POST …/gcdm/oauth/token` met `code_verifier`. Scope
  `authenticate_user openid cardata:api:read`. Vraag `cardata:streaming:read` alleen als de client op
  "CarData Stream" is geabonneerd — anders geeft BMW een kale `400 invalid_request`.
- **Tokens**: access en id token 1 uur; **refresh token 14 dagen en roteert bij elke refresh**. De
  nieuwste refresh token moet dus direct worden opgeslagen, en er mag maar één eigenaar zijn
  (de Lambdas, via Secrets Manager). Bij uurpollen wordt vanzelf ruim binnen 14 dagen ververst.
- **Koppelen via de webinterface** (`kmlog/carlink.py`, pagina *Auto*): Client ID invullen →
  device code aanvragen; de lopende login (device code + PKCE-verifier, ~5 min geldig) staat als
  `pending_login` in het secret → gebruiker keurt goed bij BMW → knop "Ik heb bevestigd" doet één
  tokenaanvraag (géén wachtlus in de Lambda) → mappings ophalen; precies één `PRIMARY`-voertuig
  wordt automatisch gekozen, bij meer kiest de gebruiker. Ontkoppelen verwijdert de container
  (best effort) en wist tokens, VIN en container; de Client ID blijft staan. Zolang er niets
  gekoppeld is, slaat de poller over zonder fout (geen alarm na een verse deploy).
- **API**: `https://api-cardata.bmwgroup.com`, headers `Authorization: Bearer …`, `x-version: v1`.
  Voertuigen: `GET /customers/vehicles/mappings` geeft een **kale lijst** (niet `{"mappings": …}`);
  alleen `PRIMARY`-voertuigen geven data.
- **telematicData vereist een container**: eenmalig `POST /customers/containers` met
  `technicalDescriptors`, daarna `GET /customers/vehicles/{vin}/telematicData?containerId=…`.
  Max 10 containers per account. Respons: `{"telematicData": {"<descriptor>": {"value": "<string>",
  "unit": …, "timestamp": …}}}` — **value is altijd een string**.
- **Descriptors** die kmlog gebruikt:
  - kilometerstand: `vehicle.vehicle.travelledDistance` (km)
  - positie: `vehicle.cabin.infotainment.navigation.currentLocation.latitude` en `.longitude`
- **Quotum**: **50 REST-calls per 24 uur** per account; ook mappings- en container-calls tellen mee
  (auth-calls niet); reset 00:00 UTC; 429 of 403 `CU-429`. Andere 403's: `CU-103` (scope),
  `CU-104` (VIN niet van jou), `CU-105` (containerId).
- **Regio**: het voertuig moet aan een EU-markt gekoppeld zijn.
- **Geen historie**: REST geeft alleen de actuele toestand. Een gemiste poll is niet achteraf op te
  halen; daarom alarms, en daarom het ontwerp met parkeer-episodes (robuust tegen gemiste
  tussenstops).

## 7. Afleiding en regels

### 7.1 Van observaties naar legs

1. **Observation**: per poll `(polled_at, odometer_km, odometer_ts, lat, lon, loc_ts)`.
   Gebruik `odometer_ts` (moment waarop de auto het meldde), niet `polled_at`.
2. **ParkingEpisode**: opeenvolgende observaties met dezelfde kilometerstand = auto staat stil,
   op positie `(lat, lon)` van de observatie, van `first_seen` (= `odometer_ts` van de eerste
   observatie: moment van melden bij parkeren) tot `last_seen` (= `polled_at` van de laatste
   observatie: het laatste moment waarop we weten dat hij er nog stond; `odometer_ts` blijft bij
   stilstand gelijk en kan dat niet vertellen).
   Gematcht aan een `Location` als de afstand ≤ `radius_m` (default 200 m).
3. **Leg**: tussen twee opeenvolgende episodes: `distance_km = odometer(ep2) − odometer(ep1)`,
   `from = ep1.location`, `to = ep2.location`, `departed_after = ep1.last_seen`,
   `arrived_at = ep2.first_seen`. Een leg kan onzichtbare tussenstops (< 1 uur) bevatten; dat is
   per ontwerp zo.
   - **Doorrijpunt**: een episode op een *onbekende* plek is geen stop als de auto er aantoonbaar
     korter dan 10 minuten stond (`last_seen − first_seen`; bij één poll: de melding is maar
     minuten oud, dus de auto was wakker). De auto meldt ook tijdens het rijden (§10); zo'n meting
     zou anders een rit opknippen in "klant → onbekend → thuis" met een onterechte vraag.
     Doorrijpunten vallen uit de keten en staan als `waypoints` bij de leg. Op een **bekende** plek
     (thuis, klant) blijft een episode altijd een stop: een meting bij vertrek van huis houdt een
     privéritje los van het woon-werkverkeer. Een doorrijpunt als laatste episode (auto rijdt nu)
     wacht op de volgende stop.
4. **Dag** van een leg = kalenderdag (Europe/Amsterdam) van vertrek **van huis** voor de rit waar
   de leg bij hoort. Een nachtdienst (heen 2 jul 20:10, terug 3 jul 08:30) telt zo één keer, op 2 jul
   (voorbeeld F). Legs zonder voorafgaand vertrek van huis, of meer dan een dag erna, krijgen hun
   eigen vertrekdag.

Uurpollen is voldoende omdat elke stop ≥ 1 uur zichtbaar is (nacht thuis, uren bij de klant), en
kortere stops in de kilometers van de leg zitten.

### 7.2 Classificatie

Regels 3 en 4 gaan over een terugrit *met* stop, dus er wordt per **segment** geclassificeerd: een
reeks legs tussen twee stops op een home/client/business-locatie, met nul of meer
privé-/onbekende stops ertussen. Eén leg = regel 1/2; meer legs = regel 3/4; geen bekend zakelijk
paar (of begin = eind) = regel 5. Een zakelijk paar zonder route in de stamdata telt 0 km tot
bevestiging (vlag `no_route`).

Stamdata (TOML, zie `data/samples/stamdata.example.toml`, of via de webinterface):

- `[vehicle]`: naam en kenteken voor de kop van de maandstaat. Het voertuig-id is `car`
  (instelbaar met `kmlog --vehicle` / Terraform `vehicle_id`).
- `Location` (type `home` | `client` | `business` | `private`).
- `Route` per paar locaties met `distance_km`, naam (bijv. "A4", "via Amsterdam") en één
  `is_default` (de standaardroute). Tolerantie `route_tolerance_km` = 5.
- `Rate` per jaar in centen per km.

Regels, in volgorde:

| # | Situatie | Uitkomst |
|---|---|---|
| 1 | Beide eindpunten bekend, `distance_km` binnen ±tolerantie van een `Route` voor dat paar | **Zakelijk**, volledige km, routelabel. Geen vraag. (Dekt omwegen door file en korte onzichtbare privéstops.) |
| 2 | Beide eindpunten bekend (home/client/business), geen route past | Zakelijk = **standaardroute-km**, rest privé. Status `needs_confirmation` met twee knoppen: *Omweg (zakelijk, alle km)* / *Privé-stop (standaard)*. Niet beantwoord na `confirm_timeout_days` (default 7) → standaard blijft staan. |
| 3 | Eén eindpunt is een `private`-locatie (zichtbare stop ≥ 1 uur op bekende privéplek) | Leg zelf privé. Het traject klant→privé→thuis wordt als één retour behandeld: zakelijk = standaardroute klant→thuis, de rest privé. Geen vraag. |
| 4 | Eén eindpunt onbekend (zichtbare stop ≥ 1 uur op onbekende plek) | Vraag: *Zakelijk* (leg volledig zakelijk; aanbieden om de plek als `business`-locatie op te slaan) / *Privé* (zoals regel 3; aanbieden als `private` op te slaan). Default bij geen antwoord: privé. |
| 5 | Beide eindpunten onbekend, of van/naar home zonder bekende bestemming | Privé tenzij bevestigd zakelijk. Vraag alleen op werkdagen en bij ≥ 20 km. |
| 6 | Auto in klantzone op weekend of feestdag | Volgens regel 1–2, met vraag *Bevestig* / *Privé* (vlag `weekend_or_holiday`). |
| 7 | Dag met gereden km maar zonder enkele zakelijke leg, op een werkdag | Informatieve melding "gemist?" (geen vergoeding zonder bevestiging). Alleen per mail als dat in Instellingen aanstaat (standaard uit). |
| 8 | Kilometerstand-delta van een dag wijkt > 20% af van de som van de legs | Datakwaliteitsvlag (gemiste poll); handmatige controle. |

Dagen zonder autorit: de auto beweegt niet → geen episodes, geen legs, niets te doen.

### 7.3 Voorbeelddagen (testfixtures)

Stamdata voor de voorbeelden (verzonnen): `home`, `client`. Routes home↔client: A4 **70** km
(default), via Amsterdam **84** km, N11 **77** km. Tolerantie 5 km.

| Dag | Legs (km) | Verwerking | Zakelijk |
|---|---|---|---|
| A. Normaal | home→client 71; client→home 70 | beide match A4 | **141** |
| B. Omweg door file | 71; client→home 85 | 85 match "via Amsterdam" (84) | **156**, geen vraag |
| C. Lange privé-omweg, stop zichtbaar | 71; client→onbekend 38 (stop 17:40–21:10); onbekend→home 51 | regel 4, antwoord *Privé* → retour = standaard 70 | **141** (van 160 gereden) |
| C'. Zelfde dag, antwoord *Zakelijk* | idem | legs volledig zakelijk, plek opslaan als `business` | **160** |
| D. Korte onzichtbare stop | 71; client→home 73 | 73 binnen 70±5 | **144** |
| E. Dag zonder auto | geen kilometerstandwijziging | niets | **0** |
| F. Nachtdienst | home→client 2 jul 20:10 (70); client→home 3 jul 08:30 (70) | één dag, 2 jul | **140** op 2 jul |
| G. Onbekende omweg zonder zichtbare stop | 71; client→home 96 | geen route (dichtstbij 84, +12) → regel 2, vraag; default standaard | **141** (of 167 na *Omweg*) |
| H. Weekend bij klant | zaterdag 70; 70 | regel 1 + vraag weekend | **140**, ter bevestiging |
| I. Privé-dag | home→onbekend 23; onbekend→home 23 | regel 5, default privé; vraag (werkdag, ≥20 km) | **0** tenzij bevestigd |
| J. Gemiste poll | episodes ontbreken, dagdelta 141 maar legs 0 | regel 8, vlag | handmatig |

## 8. Datamodel

DynamoDB single-table (`PK=VEHICLE#<id>`, `SK=OBS#…` / `LEG#<date>#<leg id>` / …; stamdata onder
een eigen partitie). Lokaal dezelfde records als JSON in SQLite.

- **Vehicle**: `vin` (= voertuig-id, default `car`), `name`, `plate`, `kind` (`car`|`motor`),
  `reimbursable`, `active_from/to`.
- **Rate**: `year`, `cents_per_km`.
- **Location**: `id`, `name`, `type` (`home`|`client`|`business`|`private`), `lat`, `lon`,
  `radius_m` (200), `active_from/to`.
- **Route**: `id`, `location_a`, `location_b` (ongeordend paar), `name`, `distance_km`, `is_default`.
- **Observation**: `vehicle`, `polled_at`, `odometer_km`, `odometer_ts`, `lat`, `lon`, `loc_ts`, `raw`.
- **ParkingEpisode**: `vehicle`, `odometer_km`, `lat`, `lon`, `location_id?`, `first_seen`, `last_seen`.
- **Leg**: `id`, `vehicle`, `date` (Amsterdam, vertrekdag), `from_location?`, `to_location?`,
  `from_latlon`, `to_latlon`, `departed_after`, `arrived_at`, `distance_km`, `matched_route?`,
  `business_km`, `private_km`, `classification` (`business`|`private`|`mixed`),
  `status` (`auto`|`needs_confirmation`|`confirmed`|`timed_out`), `flags[]`, `note`,
  `source` (`cardata`|`manual`), `odometer_from/to`, `waypoints[]`. Ritten uit oudere databases
  met een spreadsheet-bron worden als `manual` ingelezen.
- **DayNote**: `vehicle`, `date`, `text`.
- **Antwoorden en correcties**: eigen records per segment resp. leg (§9).
- **QuotaCounter**: per UTC-dag, `used`.
- **Secrets** (Secrets Manager, niet in DynamoDB): `client_id`, tokens, `vin`, container id.

Geld: altijd in centen (int). Bedrag = km × cents, in euro's getoond met twee decimalen.
Tijd: opgeslagen in UTC, getoond in `Europe/Amsterdam`.

## 9. Uitvoer en bevestigingsflow

**Maandstaat** (.xlsx): voertuig, maand, tarief; per dag: datum, omschrijving (bijv.
"Thuis → Klant → Thuis"), route(s), gereden/zakelijke/privé km, opmerking/vlag; totaal km, bedrag;
kilometerstand begin en einde maand; lijst van openstaande bevestigingen (die tellen conservatief
mee). Opslag in S3 (`reports/<jaar>/<jaar>-<maand>.xlsx`), mail via SES op de 1e.

**Ritten**: dezelfde legs één per regel, om in te zien — de declaratie blijft per dag. Per rit:
datum, "vertrokken na" (laatste poll waarin de auto nog stond; het echte vertrek ligt daartussen) en
aankomst, van → naar, route, km-stand vertrek/aankomst, gereden/zakelijk/privé, opmerking.
Handmatige ritten zijn één regel zonder tijden. Webinterface: schakelaar "Per dag · Per rit" op de
maandpagina; Excel: tweede tabblad "Ritten <maand> <jaar>".

**Jaaroverzicht**: per maand ritdagen, km en bedrag, plus totaal.

**Bevestigen**: zo weinig mogelijk vragen.

- **Kanaal = e-mail via SES** (zelfde geverifieerde adres als de maandstaat). `kmlog-notify` draait
  dagelijks om 20:00: herberekent de laatste 31 dagen (antwoorden opnieuw toepassen, vragen ouder
  dan 7 dagen → `timed_out`, standaard blijft), en stuurt **alleen als er iets nieuws is** één mail
  met de nieuwe vragen en, als dat in Instellingen aanstaat, "gemist?"-dagen (regel 7) plus een
  link naar de webinterface. Staat het uit, dan worden "gemist?"-dagen stil als verwerkt onthouden
  (aanzetten mailt geen achterstand). Een vraag wordt nooit twee keer gemaild.
- **Webinterface** = Lambda `kmlog-web` achter een **Function URL**, server-side HTML, geen
  JavaScript, mobiel eerst. Pagina's: maand (ritten per dag of per rit, totaal, bedrag,
  Excel-download, openstaande vragen met één-tik-knoppen, laatste positie van de auto), dag (legs met
  tijden en een kaartje van de aankomstplek, zakelijke km corrigeren met notitie, handmatige rit
  toevoegen/verwijderen, antwoord intrekken, alle metingen), jaar, instellingen (auto, vergoeding per
  jaar, uitleesvenster, meldingen, locaties en routes), auto (koppelen/ontkoppelen, zie §6).
  Een locatie voeg je toe met coördinaten of via zoeken op adres/bedrijf (zie hieronder).
- **Inloggen**: één gebruiker, geen wachtwoord. Knop "stuur inloglink" → mail naar het vaste adres
  (geen invoerveld) met een eenmalige link (256-bit token, alleen de hash opgeslagen, 15 minuten,
  max. 5 mails per uur). De link opent een pagina met een knop; pas die POST verbruikt het token
  (mailscanners die links voorladen kunnen het dus niet opmaken). Daarna een HMAC-ondertekende
  sessiecookie (30 dagen, HttpOnly, Secure, SameSite=Lax), CSRF-token op elk formulier plus
  Origin-controle, strikte CSP. Sleutel in Secrets Manager `kmlog/web`, gegenereerd door Terraform.
- **Antwoorden overleven herberekening**: antwoorden (per segment, sleutel = id van de eerste leg)
  en correcties (per leg) staan in eigen records; elke herberekening past ze opnieuw toe. Een
  antwoord dat niet meer bij de vraag past (bijv. omdat een stop inmiddels een bekende locatie is)
  wordt genegeerd en kan dus nooit méér km opleveren.
- **Onbekende stop opslaan**: als zakelijke locatie → routes naar/van die plek worden aangemaakt met
  de werkelijk gereden km (de rit telt volledig, volgende keer zonder vraag); als privé → regel 3.
- **Locatie zoeken**: `/instellingen/zoek?q=` laat de Lambda Nominatim (OpenStreetMap) bevragen en
  toont maximaal vijf resultaten met adres; "Kies deze" opent `/instellingen/nieuw` met naam en
  coördinaten voorgevuld en een kaartje, en opslaan gaat via dezelfde POST als een handmatige
  locatie. Alleen op een expliciete zoekopdracht (de gebruiksregels van Nominatim verbieden
  autocomplete), met een limiet van 1 verzoek/s (teller in de store), een cache in het geheugen van
  de Lambda, voorkeur voor resultaten rond thuis (`viewbox`, niet beperkend) en zonder
  bushaltes/straten (`category=highway`). Bij een storing blijft het handmatige formulier.
- **Handmatige rit**: via de dagpagina, voor als de auto geen data gaf.
- **Stamdata**: na bewerken in de webinterface is de database de bron. `kmlog stamdata --export`
  schrijft de database naar TOML; `kmlog stamdata` (import) weigert een afwijkende database te
  overschrijven zonder `--force`.

**Mail en DKIM**: als het afzenderdomein DMARC `p=reject` publiceert, moet de mail DKIM-ondertekend
zijn door dat domein. Standaard Easy DKIM (drie CNAME-records, Terraform-output
`dkim_dns_records`); voor DNS-providers die geen CNAME onder `_domainkey` kunnen maken optioneel
Bring Your Own DKIM via `dkim_private_key_file` (één TXT-record).

## 10. Meldgedrag en bekende beperkingen

Gezien met een BMW i4 via REST:

- Bij parkeren komt een verse kilometerstand + positie; de tijdstempel is het moment van aankomst en
  blijft staan zolang de auto stilstaat. Kilometerstand, lat en lon hebben dezelfde tijdstempel.
- De auto meldt **ook tijdens het rijden** of als hij wakker is (bijv. een melding 2 km voor huis,
  midden in de rit). Opgelost met doorrijpunten (§7.1).
- `vehicle.isMoving` en contact (`vehicle.drivetrain.engine.isActive` / `isIgnitionOn`) blijven
  leeg (lege `value` én `timestamp`), ook midden in een rit. Ze worden daarom niet opgevraagd.
- Inpluggen van de laadkabel geeft een verse melding (zelfde stand).
- Een korte stop (bijv. 40 minuten) is onzichtbaar als de auto binnen hetzelfde uur weer rijdt; de
  km zitten dan in de leg en vallen meestal binnen de routemarge (regel 1) of geven een vraag (regel 2).
- Door het uurpollen is een leg niet altijd één echte rit (bijv. boodschap + vertrek = 3 km
  thuis → thuis); daarom "vertrokken na" in plaats van een vertrektijd.

Andere modellen kunnen anders melden; de afleiding gebruikt alleen kilometerstand, positie en
tijdstempels.

## 11. Beslislog

| Datum | Beslissing | Reden |
|---|---|---|
| 2026-10-10 | Auto (niet telefoon) als primaire sensor | Onderscheidt autodagen van ander vervoer; harde kilometers. |
| 2026-10-10 | Geen Home Assistant | Geen meerwaarde; één systeem in de cloud i.p.v. twee. |
| 2026-10-10 | Officiële BMW CarData API, geen `bimmer_connected` | Officieel, geen captcha/reverse engineering. |
| 2026-10-10 | Werkelijke kilometerstand i.p.v. vaste afstanden | Routes variëren. Vaste routes blijven referentie. |
| 2026-10-10 | Pollen 1× per uur (06–23), niet elke 10 min | Quotum 50/24u; uren stilstand maakt vaker zinloos. |
| 2026-10-10 | Tussenstopregel: route-matching ±5 km; anders standaardroute zakelijk, rest privé; één knop om te overrulen | Omwegen door file automatisch goed; privé-omwegen conservatief; minimale interactie. |
| 2026-10-10 | Conservatieve default bij onbeantwoorde vraag | Nooit te veel declareren. |
| 2026-10-10 | AWS, serverless, Terraform | Kosten ≈ 0; reproduceerbaar. |
| 2026-10-10 | Episode: `first_seen` = `odometer_ts`, `last_seen` = laatste `polled_at` | `odometer_ts` bevriest bij stilstand; alleen de poll weet dat de auto er nog stond. |
| 2026-10-10 | Dag van een leg = vertrekdag van huis van de rit | Anders landt de terugrit van een nachtdienst op de volgende dag (voorbeeld F vereist 140 op 2 jul). |
| 2026-10-10 | Classificatie per segment (legs tussen twee zakelijke locaties) i.p.v. per leg | Regels 3/4 gaan over klant→stop→thuis samen; standaardroute-km worden over de legs verdeeld. |
| 2026-10-10 | Zakelijk paar zonder bekende route → 0 km tot bevestiging | Conservatief (R6); nieuwe route toevoegen lost het blijvend op. |
| 2026-10-10 | Alleen scope `cardata:api:read`, geen streaming | Zonder stream-abonnement geeft de device code een 400; uurpollen via REST heeft hem niet nodig. |
| 2026-10-10 | Poller doet ook de afleiding (gisteren + vandaag, idempotent); geen aparte derive-Lambda | Eén functie minder; afleiding kost milliseconden. |
| 2026-10-10 | Quotumteller per UTC-dag in DynamoDB, harde stop op 40 calls | BMW-limiet 50/24u (reset 00:00 UTC); marge voor handmatige calls. Refresh telt niet mee. |
| 2026-10-10 | Geen retries op de poller (Scheduler én async Lambda) | Een gemiste poll is goedkoper dan verspild quotum; het volgende uur komt vanzelf. |
| 2026-10-10 | IAM-rollen met een permissions boundary | Deploy-gebruiker kan zo geen rol met meer rechten maken (geen escalatie via Lambda). |
| 2026-10-10 | Terraform-state in S3 met `use_lockfile` (geen DynamoDB-lock) | Terraform ≥ 1.10 ondersteunt S3-native locking; één resource minder. |
| 2026-10-10 | Budget `kmlog-monthly` ($5) op tag `project=kmlog` | Een account kan ook andere workloads draaien; tag moet als cost allocation tag geactiveerd zijn. |
| 2026-10-10 | Bevestigen via dagelijkse e-mail (SES) + webinterface | SES is er al voor de maandstaat; geen extra app of bot; één mail per dag alleen als er iets nieuws is. |
| 2026-10-10 | Webinterface als Lambda Function URL, server-side HTML zonder JS | Goedkoopst en simpelst (geen API Gateway/S3-site/CloudFront); geen build-stap; strikte CSP mogelijk. |
| 2026-10-10 | Inloggen met eenmalige maillink + ondertekende cookie, geen wachtwoord | Eén gebruiker; de mailbox is al het vertrouwde kanaal; geen wachtwoordbeheer. Link alleen naar het vaste adres. |
| 2026-10-10 | Sessiesleutel door Terraform gegenereerd (random_password → `kmlog/web`) | Lambda zelf laten aanmaken vraagt `secretsmanager:CreateSecret`, buiten de boundary. Sleutel staat ook in de (versleutelde) state. |
| 2026-10-10 | Antwoorden en correcties apart opslaan, bij elke herberekening opnieuw toepassen | Legs worden elk uur opnieuw afgeleid; anders zou elk antwoord verdwijnen. Niet-passende antwoorden worden genegeerd (R6). |
| 2026-10-10 | Weekend/feestdag bij klant = vraag (Bevestig / Privé) | Eén tik, en verloopt net als de rest. |
| 2026-10-10 | Onbekende stop als zakelijk opslaan maakt routes met de gereden km | Anders telt een rit naar een nieuwe zakelijke locatie 0 km tot er handmatig een route is. |
| 2026-10-12 | Doorrijpunten: onbekende plek met < 10 min aantoonbare stilstand = geen stop | Een poll midden in de terugrit gaf een onterechte vraag. Korte stops vallen zo net als ongeziene stops binnen de routemarge; grote omwegen blijven opvallen (regel 2). |
| 2026-10-12 | Straal thuis ruim genoeg kiezen (bijv. 300 m) | Een meting bij vertrek kan net buiten 200 m liggen; dan wordt een privérit bij het woon-werkverkeer opgeteld. |
| 2026-10-12 | Geen CarData Stream (MQTT) | Vraagt een altijd-open verbinding (enkele dollars per maand vs < $1 nu, één verbinding per account, tokens delen) voor weinig winst in de declaratie. |
| 2026-10-14 | Locaties zoeken op adres of bedrijf via de publieke Nominatim, alleen OpenStreetMap, zonder waarschuwing of blokkade in de webinterface | Typen van coördinaten is de grootste drempel bij het instellen. Nominatim vindt bedrijven en adressen goed, maar postcode + huisnummer slecht; PDOK (alleen adressen) is daarom bewust niet toegevoegd. De zoekterm gaat naar OSMF; dat staat in de README en infra/README, en zoeken is uit te zetten met `geocoder_url = "off"`. Een radius-suggestie uit de bounding box is weggelaten: voor bedrijven is die ~10 m. |
| 2026-10-12 | Ritten-overzicht naast de maandstaat (web-schakelaar + Excel-tabblad), declaratie blijft per dag | Per rit inzien is handig; de dagregels zijn wat de BV betaalt. Een leg is door het uurpollen niet altijd één echte rit; daarom "vertrokken na". |
| 2026-10-13 | `isMoving` en contact niet in de container; doorrijpunt alleen op tijd | Een hele rit lang leeg, ook tijdens het rijden; de i4 levert ze niet via REST. |
| 2026-10-13 | `Referrer-Policy: same-origin` (header, geen `<meta name="referrer">`) | Met `no-referrer` sturen browsers `Origin: null` bij elk formulier; de herkomstcontrole weigerde daardoor álle acties na het inloggen. |
| 2026-10-13 | Dagpagina: kaartje van de aankomstplek per rit; doorrijpunten niet in de overzichten | Doorrijpunten voegen in het overzicht niets toe; de metingentabel op de dagpagina toont ze nog. |
| 2026-10-13 | Voertuig (naam, kenteken) in de stamdata; voertuig-id default `car` | Eén plek voor alle configuratie; geen import nodig om een maandstaat te kunnen maken. |
| 2026-10-13 | Easy DKIM als default, Bring Your Own DKIM optioneel | Easy DKIM werkt bij de meeste DNS-providers; BYODKIM alleen voor providers zonder CNAME onder `_domainkey`. |
| 2026-10-13 | Auto koppelen via de webinterface i.p.v. lokaal script + `.env` | Geen lokale BMW-tokens of scripts nodig; de device-code flow past op twee knoppen (aanvragen, bevestigen) zonder wachtlus in de Lambda. Eén auto per installatie. |
| 2026-10-13 | Vergoeding per jaar in de webinterface (tabel, toevoegen/wijzigen/verwijderen); jaar zonder tarief gebruikt het laatste eerdere | Een nieuw jaar mag de maandstaat van januari niet blokkeren; wijkt het tarief af, dan stel je het in. |
| 2026-10-13 | Uitleesvenster instelbaar in de webinterface (standaard 06–23); de scheduler roept de poller elk uur aan en die slaat uren buiten het venster over vóór elke BMW-call | Wie 's nachts rijdt, wil ook dan metingen; 24 polls per dag passen in het quotum (50, eigen grens 40). Zonder deploy aan te passen; een overgeslagen uur kost alleen een Lambda-aanroep. De webinterface weigert een venster dat niet past. |
| 2026-10-13 | Pagina "Stamdata" heet "Instellingen" (`/instellingen`, `/stamdata` stuurt door) | Er staan nu ook auto, tarief en uitleesvenster op; "stamdata" blijft de term voor locaties, routes en het TOML-bestand. |
| 2026-10-13 | "gemist?"-mail (regel 7) uitzetbaar in Instellingen, standaard uit | Elke privérit op een werkdag gaf een mail zonder iets te beantwoorden (ook onder de 20 km van regel 5); de mail moet alleen komen als er iets te doen is. |
| 2026-10-21 | Versies: git-tag `vX.Y.Z` = code + infra, GitHub Release met `lambda.zip`, `scripts/deploy.sh` installeert of zet terug | Bijwerken en terugzetten naar een bekende versie moet één commando zijn, met exact het geteste bestand. Geen aparte testomgeving: de auto is maar door één installatie uit te lezen (tokens roteren, quotum 50/dag). |
| 2026-10-21 | Opgeslagen gegevens alleen uitbreiden (geen velden hernoemen of verwijderen, geen nieuwe enum-waarden zonder vermelding) | Terugzetten moet de gegevens van een nieuwere versie kunnen lezen. Ritten worden elk uur opnieuw berekend, dus alleen antwoorden, correcties en stamdata blijven staan. |

## 12. Bronnen

- BavarianData wiki (Home Assistant-integratie voor BMW CarData): https://github.com/JustChr/BavarianData/wiki
- API-quotum 50 calls/24u: https://github.com/JustChr/BavarianData/wiki/Feature-API-Quota
- Portaal-setup, Client ID en scopes: https://github.com/JustChr/BavarianData/wiki/Getting-Started-1-BMW-Portal-Setup
- Datapunten/stream: https://github.com/JustChr/BavarianData/wiki/Getting-Started-4-Choose-Data
- bmw-cardata-ha (incl. kopie van BMW's API-docs/Swagger): https://github.com/kvanbiesen/bmw-cardata-ha
- HA-community, CarData via MQTT, veldnamen: https://community.home-assistant.io/t/bmw-cardata-directly-via-mqtt/999864
- bimmer_connected discussie #745 over de CarData API: https://github.com/bimmerconnected/bimmer_connected/discussions/745
- bmw-mqtt-bridge (referentie-implementatie login + stream): https://github.com/dj0abr/bmw-mqtt-bridge
- BMW Group over CarData: https://www.bmwgroup.com/en/innovation/connected-car/cardata.html
- Officiële API-documentatie (achter login): https://bmw-cardata.bmwgroup.com/customer/public/api-documentation/Id-Introduction

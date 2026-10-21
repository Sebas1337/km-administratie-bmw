# Wijzigingen

Versies volgen [SemVer](https://semver.org/lang/nl/): in 0.x is een minor-versie (0.2.0) nieuwe
functionaliteit en een patch (0.2.1) een reparatie. Elke versie is een git-tag `vX.Y.Z` met een
GitHub Release (`lambda.zip`); installeren, bijwerken of terugzetten gaat met
`scripts/deploy.sh vX.Y.Z` (zie README, *Bijwerken*).

## [Unreleased]

### Toegevoegd
- Optioneel eigen domein voor de webinterface (`web_domain`), via CloudFront met een
  ACM-certificaat; ook de links in de mails gebruiken het dan. Nodig: een DNS-provider die
  CNAME-records met een underscore kan maken, zoals Cloudflare. Zie infra/README, *Eigen domein*
  (PR #3, door @dutchdinosaur639).

## [0.3.2] - 2026-10-21

### Gerepareerd
- De maandpagina toont weer alle dagen en ritten, ook die alleen privé zijn (de ingang naar de
  dagpagina). Alleen de Excel laat ze weg.

## [0.3.1] - 2026-10-21

### Gewijzigd
- Dagen en ritten die alleen privé zijn, staan niet meer op de maandstaat (Excel en
  maandpagina). Ze staan op de dagpagina; de lijsten in de maandmail blijven.
- Zonder ingesteld tarief geldt vanaf 2026 € 0,25 per km; het formulier stelt € 0,25 voor.

## [0.3.0] - 2026-10-21

### Gewijzigd
- **Afstanden worden geleerd** in plaats van ingevuld: per paar plekken de meest voorkomende
  afstand van de laatste 10 aannemelijke ritten. Een rit tot 15% langer telt volledig zakelijk
  (was ±5 km van een route); de eerste rit naar een nieuwe plek telt als hij aannemelijk is
  (1×–2× hemelsbreed). Routes invullen kan niet meer; Instellingen toont de geleerde afstanden.
- **Onbekende plekken zijn privé zonder vraag**, ook op werkdagen. Een tussenstop op een
  onbekende plek telt als privé-stop (zakelijk = verwachte afstand), zonder vraag.
- **Weekend en feestdag zijn altijd privé**, ook bij een klant, met de knop *Toch zakelijk* op de
  dagpagina. Voorheen telde zo'n rit zakelijk tot je *Privé* koos.

### Toegevoegd
- Op de dagpagina bij elke onbekende plek: *Opslaan als klant / zakelijk / privé*; de ritten
  vanaf de 1e van de vorige maand worden opnieuw berekend.
- Maandmail en maandpagina: *Onbekende plekken* (werkdagen, vanaf 10 km) en *Weekendritten naar
  klanten*, met links naar de dagen.
- Per rit een korte uitleg (dagpagina, maandpagina per rit, Excel-tabblad "Ritten").
- Maandpagina: "gewijzigd na verzending" met de knop *Opnieuw mailen*.
- `kmlog vergelijk VAN TOT`: ritten op een kopie opnieuw indelen en de verschillen tonen.

## [0.2.1] - 2026-10-21

### Beveiliging
- Inloggen vraagt je e-mailadres. Alleen het adres van de installatie (`report_email`) krijgt een
  inloglink en alleen die aanvragen tellen mee voor de limiet van 5 per uur. Bots die de site
  vinden kunnen je zo niet meer mailen of buitensluiten.

## [0.2.0] - 2026-10-21

### Toegevoegd
- Versiebeheer: release per git-tag met `lambda.zip` (GitHub Actions), `scripts/deploy.sh vX.Y.Z`
  om een bepaalde versie te installeren of terug te zetten. De versie staat onderaan de
  webinterface, in elke logregel en in `kmlog --version`.
- Instellingen: locatie zoeken op adres of bedrijfsnaam (OpenStreetMap) en het formulier
  "locatie toevoegen" voorgevuld openen, met kaartje (PR #4).

## [0.1.0] - 2026-10-13

Eerste versie, zoals live sinds 10 oktober 2026 (stand van PR #2).

- Elk uur kilometerstand en positie via BMW CarData; ritten afleiden met doorrijpunten.
- Indeling zakelijk/privé met locaties en routes (ONTWERP §7), vragen ter bevestiging.
- Webinterface: maand, dag, jaar, instellingen, auto koppelen; inloggen met een maillink.
- Maandstaat en jaaroverzicht als Excel, maandelijks per mail; dagelijkse melding van open vragen.
- "gemist?"-mail als instelling, standaard uit.

[Unreleased]: https://github.com/Sebas1337/km-administratie-bmw/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/Sebas1337/km-administratie-bmw/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/Sebas1337/km-administratie-bmw/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Sebas1337/km-administratie-bmw/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/Sebas1337/km-administratie-bmw/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Sebas1337/km-administratie-bmw/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Sebas1337/km-administratie-bmw/releases/tag/v0.1.0

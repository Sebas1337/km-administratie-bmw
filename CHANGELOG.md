# Wijzigingen

Versies volgen [SemVer](https://semver.org/lang/nl/): in 0.x is een minor-versie (0.2.0) nieuwe
functionaliteit en een patch (0.2.1) een reparatie. Elke versie is een git-tag `vX.Y.Z` met een
GitHub Release (`lambda.zip`); installeren, bijwerken of terugzetten gaat met
`scripts/deploy.sh vX.Y.Z` (zie README, *Bijwerken*).

## [Unreleased]

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

[Unreleased]: https://github.com/Sebas1337/km-administratie-bmw/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Sebas1337/km-administratie-bmw/releases/tag/v0.1.0

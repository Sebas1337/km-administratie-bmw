# infra: AWS (Terraform)

Installatiestappen staan in de [README](../README.md#installeren). Dit is het naslagwerk.

Serverless, standaard in regio `eu-central-1`, alles met de naam `kmlog-*`:

| Resource | Naam | Wat |
|---|---|---|
| DynamoDB | `kmlog-data` | single table: stamdata, observaties, legs, antwoorden, quotumteller (PITR aan, deletion protection) |
| Secrets Manager | `kmlog/cardata` | BMW Client ID, roterende tokens, VIN, container id; geschreven door de webinterface (pagina *Auto*) en de poller, nooit door Terraform |
| Secrets Manager | `kmlog/web` | sleutel voor sessiecookies (door Terraform gegenereerd) |
| S3 | `kmlog-reports-<account>-<regio>` | maandstaten `reports/<jaar>/…xlsx` |
| Lambda | `kmlog-poller` | elk uur; binnen het venster uit de webinterface (Instellingen → Uitlezen, standaard 06–23): token refresh, quotum, telematicData → observatie → legs |
| Lambda | `kmlog-report` | 1e van de maand 06:00: maandstaat (+ jaaroverzicht in januari) → S3 + mail |
| Lambda + Function URL | `kmlog-web` | webinterface; inloggen met maillink; output `web_url` |
| Lambda | `kmlog-notify` | dagelijks 20:00: nieuwe vragen mailen, vragen ouder dan 7 dagen laten verlopen |
| Scheduler | `kmlog-poll` (elk uur), `kmlog-monthly-report`, `kmlog-notify` | cron in `Europe/Amsterdam`; geen retries op de poller (quotum) |
| CloudWatch/SNS | `kmlog-alerts` + alarms | fouten van poller/report/web/notify, poller minder dan 16 keer aangeroepen in 24 uur |
| Budgets | `kmlog-monthly` | mail bij > 80% (prognose) / 100% van $5 per maand, alleen kosten met tag `project=kmlog` (tag eenmalig activeren: Billing → Cost allocation tags) |

IAM: elke rol krijgt de permissions boundary `kmlog-boundary` (variabele
`boundary_policy_name`), zie [`iam/README.md`](iam/README.md). Terraform-state: S3-bucket
`kmlog-tfstate-<account>-<regio>` (gemaakt door [`bootstrap/`](bootstrap/main.tf)) met S3-native
locking.

## Variabelen

Zie [`variables.tf`](variables.tf); zet ze in `infra/terraform.tfvars` (gitignored).

| Variabele | Standaard | |
|---|---|---|
| `report_email` | — | ontvanger én afzender van alle mail (moet op een domein staan dat je beheert) |
| `alert_email` | `report_email` | alarmen en budget |
| `region` | `eu-central-1` | zie *Andere regio* |
| `vehicle_id` | `car` | sleutel van observaties/legs; gelijk aan `kmlog --vehicle` |
| `quota_limit` | `40` | max CarData-calls per UTC-dag (BMW: 50) |
| `monthly_budget_usd` | `5` | |
| `boundary_policy_name` | `kmlog-boundary` | naam van de boundary-policy uit `iam/` |
| `geocoder_url` | `https://nominatim.openstreetmap.org` | server voor het zoeken van adressen en bedrijven; `off` zet zoeken uit |
| `geocoder_countries` | `nl` | landcodes waarop gezocht wordt, bijv. `nl,be,de` |
| `geocoder_contact` | de repo-URL | contact-URL in de User-Agent van zoekopdrachten |
| `dkim_private_key_file` | — | alleen voor Bring Your Own DKIM, zie hieronder |

## Bijwerken

```sh
set -a; . ./.env; set +a
./scripts/build_lambda.sh && terraform -chdir=infra apply
```

## Stamdata als bestand

De webinterface is de bron van de stamdata. Een kopie als TOML, of een bestand inlezen:

```sh
uv run kmlog --store dynamodb stamdata --export data/stamdata.toml   # database → bestand
uv run kmlog --store dynamodb stamdata data/stamdata.toml            # bestand → database
#   weigert als de database afwijkt (bijv. na wijzigingen in de webinterface); --force overschrijft
```

## Logs en handmatig draaien

```sh
aws logs tail /aws/lambda/kmlog-poller --since 1d     # één JSON-regel per poll, zonder coördinaten
aws lambda invoke --function-name kmlog-poller --cli-binary-format raw-in-base64-out \
  --payload '{"force": true}' /dev/stdout          # ook buiten het uitleesvenster
aws lambda invoke --function-name kmlog-report \
  --cli-binary-format raw-in-base64-out --payload '{"year": 2026, "month": 9}' /dev/stdout
```

Een handmatige poll kost één CarData-call van het dagquotum.

## Mail en DKIM

SES start in sandbox-modus: afzender en ontvanger moeten geverifieerd zijn. km-admin mailt alleen
van en naar `report_email`, dus productietoegang is niet nodig. Terraform maakt twee identiteiten:
het adres (klik de verificatiemail) en het domein (voor DKIM).

**Easy DKIM** (standaard): `terraform output dkim_dns_records` geeft drie CNAME-records
`<token>._domainkey.<domein>` → `<token>.dkim.amazonses.com`. Zet ze bij je DNS-provider; SES
verifieert het domein daarna vanzelf.

**Bring Your Own DKIM**: voor DNS-providers die geen CNAME onder `_domainkey` kunnen maken, wel
een TXT-record.

```sh
mkdir -p data/dkim && cd data/dkim            # gitignored
openssl genrsa -out kmlog.private.pem 2048 && chmod 600 kmlog.private.pem
openssl rsa -in kmlog.private.pem -pubout -out kmlog.public.pem
```

Zet in `terraform.tfvars` `dkim_private_key_file = "../data/dkim/kmlog.private.pem"` (en eventueel
`dkim_selector`). `terraform output dkim_dns_records` geeft dan één TXT-record
`kmlog._domainkey.<domein>`. Bewaar het sleutelbestand: zonder faalt `terraform plan`. Sleutel
vervangen: nieuw sleutelpaar, nieuw TXT-record en
`terraform apply -replace=aws_sesv2_email_identity.domain`.

## Adres zoeken

Op de pagina *Instellingen* zoek je een locatie op adres of bedrijfsnaam; het resultaat vult het
formulier "Locatie toevoegen" voor. De Lambda `kmlog-web` vraagt dat aan de publieke
[Nominatim](https://nominatim.org/)-server van OpenStreetMap, dus alleen op een expliciete
zoekopdracht van jou (geen autocomplete, niets periodiek), met maximaal één verzoek per seconde,
een User-Agent met de repo-URL en resultaten die een dag in het geheugen blijven staan. Dat past
binnen het [gebruiksbeleid](https://operations.osmfoundation.org/policies/nominatim/). De zoekterm
gaat dus naar OpenStreetMap; de pagina waarschuwt daarvoor en vraagt om geen privéadres te zoeken.
Wil je dat niet, zet `geocoder_url = "off"`; het handmatige formulier blijft werken. Een eigen
Nominatim-server of een andere compatibele dienst zet je in `geocoder_url`.

## Andere regio

1. Vervang `eu-central-1` in `iam/boundary-policy.json` en `iam/deployer-policy.json` en werk de
   policies in IAM bij.
2. `terraform -chdir=infra/bootstrap apply -var region=<regio>`.
3. `terraform -chdir=infra init -backend-config="bucket=kmlog-tfstate-$ACCOUNT-<regio>" -backend-config="region=<regio>"`
   en `region = "<regio>"` in `terraform.tfvars`.

## Verwijderen

Dit wist al je ritten, antwoorden en instellingen. Bewaar eerst wat je wilt houden: de
maandstaten (`aws s3 sync s3://kmlog-reports-<account>-<regio> ./reports-backup`) en de stamdata
(`uv run kmlog --store dynamodb stamdata --export data/stamdata.toml`). Ontkoppel de auto op de
pagina *Auto*, zodat ook de container bij BMW verdwijnt.

```sh
set -a; . ./.env; set +a
# 1. beveiliging uit: deletion protection van de tabel, bucket mag niet-leeg weg, secrets direct weg
terraform -chdir=infra apply -var allow_destroy=true
# 2. alles van de hoofd-stack weg
terraform -chdir=infra destroy -var allow_destroy=true
# 3. de state-bucket (bootstrap): eerst force_destroy aan, dan weg
terraform -chdir=infra/bootstrap apply -var allow_destroy=true
terraform -chdir=infra/bootstrap destroy -var allow_destroy=true
```

Verwijder daarna in het BMW-portaal de CarData Client, en in IAM de deploy-gebruiker en de
twee policies.

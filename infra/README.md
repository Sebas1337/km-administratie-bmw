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

## Andere regio

1. Vervang `eu-central-1` in `iam/boundary-policy.json` en `iam/deployer-policy.json` en werk de
   policies in IAM bij.
2. `terraform -chdir=infra/bootstrap apply -var region=<regio>`.
3. `terraform -chdir=infra init -backend-config="bucket=kmlog-tfstate-$ACCOUNT-<regio>" -backend-config="region=<regio>"`
   en `region = "<regio>"` in `terraform.tfvars`.

## Verwijderen

De tabel heeft deletion protection, en de buckets bevatten data. Exporteer eerst wat je wilt
bewaren (maandstaten uit S3, `kmlog stamdata --export`). Zet daarna
`deletion_protection_enabled = false` in `main.tf`, apply, leeg de buckets, en draai
`terraform -chdir=infra destroy`. Verwijder in het BMW-portaal ook de CarData Client.

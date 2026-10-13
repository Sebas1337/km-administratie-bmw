# IAM voor de deploy-gebruiker

Terraform draait met een IAM-gebruiker met beperkte rechten, niet als admin. Daarvoor zijn twee
**customer managed policies** nodig. Ze zijn niet inline, want de deployer-policy is groter dan
de 2.048 tekens die een inline user policy mag hebben.

| Naam | Bestand | Waarvoor |
|---|---|---|
| `kmlog-deployer` | `deployer-policy.json` | Aan de IAM-gebruiker hangen. Mag alleen resources met prefix `kmlog` beheren, in `eu-central-1`. |
| `kmlog-boundary` | `boundary-policy.json` | **Niet** aan iets hangen. Wordt de *permissions boundary* van elke rol die Terraform maakt. |

Waarom een boundary: wie rollen mag aanmaken en aan een Lambda mag geven, kan zichzelf via die
Lambda alle rechten geven. De deployer mag daarom alleen rollen maken (en inline policies zetten)
met `kmlog-boundary` als boundary. Wat een Lambda-rol ooit krijgt, valt altijd binnen die
boundary: DynamoDB, Secrets, S3 en logs van `kmlog`, en mail versturen. De deployer kan de
boundary niet wijzigen of verwijderen.

## Stappen (AWS-console, als admin)

1. IAM → Policies → Create policy → JSON → inhoud van `boundary-policy.json` → naam
   **`kmlog-boundary`**. Kies je een andere naam, zet die dan ook in `deployer-policy.json` en
   in de Terraform-variabele `boundary_policy_name`.
2. Idem met `deployer-policy.json` → naam **`kmlog-deployer`**.
3. IAM → Users → Create user (bijv. `kmlog-deployer`, geen console-toegang) → Attach policies
   directly → `kmlog-deployer`.
4. User → Security credentials → Create access key → *Command Line Interface (CLI)*.
5. In `.env` (gitignored):
   ```sh
   AWS_ACCESS_KEY_ID=AKIA...
   AWS_SECRET_ACCESS_KEY=...
   AWS_REGION=eu-central-1
   ```

Controle: `set -a; . ./.env; set +a; aws sts get-caller-identity`.

De bootstrap (state-bucket) draait ook met deze gebruiker. Lukt dat niet in jouw account, draai
`infra/bootstrap` dan eenmalig als admin.

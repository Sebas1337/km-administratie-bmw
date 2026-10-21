# Instructions for AI assistants

This file is for an AI (LLM) assistant that helps someone **deploy, operate or change** km-admin.
Humans: see [README.md](README.md). The user-facing docs and the web UI are in Dutch; code,
identifiers and commit messages are in English. Answer the user in their own language.

## What this is

Automatic mileage log for business trips with a private BMW. Every hour a Lambda reads the
odometer and position through the official BMW CarData API, derives trips, classifies them as
business/private against the user's places and routes, and mails a monthly Excel statement.
Serverless on AWS (Lambda Python 3.12 arm64, DynamoDB, Secrets Manager, S3, SES, EventBridge
Scheduler), Terraform in `infra/`. Design and every rule with its reason: `docs/ONTWERP.md`
(§7 = trip derivation and classification, §11 = decision log; add new decisions there).

## Deploying for a user

Follow README "Installeren" step by step; verify each step before the next. Commands run from
the repo root with the deploy credentials loaded: `set -a; . ./.env; set +a`.

- **Steps only the human can do**, ask and wait: creating the IAM policies/user and access key
  (step 1, AWS console as admin), clicking the SES verification and SNS confirmation mails
  (step 5), adding DNS records for DKIM (step 5), logging in via the mail link (step 6), linking
  the car at BMW (step 7: they must type the code shown on the Auto page into the BMW page).
- **Check the account first**: `aws sts get-caller-identity`. Never deploy into an account the
  user did not intend; if they run more than one installation, keep one working directory per
  account (the backend config in `infra/.terraform` points at one state bucket).
- **Variables** go in `infra/terraform.tfvars` (gitignored): at least `report_email`, an address
  on a domain the user controls. If their DNS provider cannot create CNAMEs under `_domainkey`,
  use Bring Your Own DKIM (`dkim_private_key_file`, see `infra/README.md`).
- **Always `terraform plan` before `apply`** and read it. Nothing in a normal update should be
  destroyed or replaced; stop and ask if the plan wants to destroy/replace the DynamoDB table,
  a secret or a bucket (that is user data).
- Verify after deploy: `aws lambda invoke --function-name kmlog-poller out.json` (expect
  `not_linked` before the car is linked, `outside_window` outside the poll window, or `poll`),
  and `curl -s -o /dev/null -w '%{http_code}' "$(terraform -chdir=infra output -raw web_url)login"`
  (expect 200). A manual poll costs one CarData call; `{"force": true}` polls outside the window.

## Versions

A version is code + infra under one git tag `vX.Y.Z` (SemVer; `pyproject.toml` `version` must
match). Pushing the tag runs `.github/workflows/release.yml`: tests, `build_lambda.sh` (writes the
tag into `kmlog/_build.py`), and a GitHub Release with `lambda.zip` + `lambda.zip.sha256`. Notes
come from the tag's section in `CHANGELOG.md`, so that section must exist before tagging.

- **Deploy a version** (also downgrade): `scripts/deploy.sh vX.Y.Z`. It checks out the tag, verifies
  the release zip, checks that the state bucket belongs to the logged-in account, runs `terraform
  apply` and returns to the previous branch. As an assistant (no interactive prompt): run
  `scripts/deploy.sh vX.Y.Z --plan`, read the plan (same rules as above: stop on destroy/replace of
  data), then `scripts/deploy.sh vX.Y.Z --apply`, which applies exactly that saved plan. Running version: footer of the web UI,
  `version` field in every log line.
- **Release**: on main, move `[Unreleased]` in `CHANGELOG.md` to `[X.Y.Z] - date`, bump
  `pyproject.toml` (and `uv lock`), commit via a PR, then `git tag vX.Y.Z && git push origin vX.Y.Z`.
- **Downgrades must stay safe**: stored data changes are additive only (new item types or fields,
  never renamed/removed/re-typed; a new enum value such as a location type also counts as a
  break, because the previous version cannot read it), and readers ignore unknown fields
  (`store/codec.py` does). Legs are re-derived every hour,
  so derivation changes roll back by themselves; answers, overrides, locations, routes and settings
  persist and must stay readable by the previous version. If a change can't meet this, say so in
  `CHANGELOG.md` and the decision log.

## Rules

- **Never print or log secrets**: AWS keys in `.env`, the CarData tokens in Secrets Manager
  `kmlog/cardata`, the session key `kmlog/web`, login links from mails. Read `.env` only by
  sourcing it; inspect a secret by its key names, not its values.
- **BMW quota**: 50 CarData calls per 24 h per BMW account (resets 00:00 UTC); km-admin stops
  itself at `quota_limit` (40). Don't poll in loops; every mapping/container/telematic call counts.
- **Refresh tokens rotate**: whoever refreshes first invalidates the other copy. Only the Lambdas
  own the tokens; never refresh them from a local script while the stack is live.
- The users' data is in DynamoDB `kmlog-data` (deletion protection, PITR). Don't write to it
  directly; use the web UI or `uv run kmlog --store dynamodb ...`.
- `terraform destroy` only when the user explicitly asks; `infra/README.md` "Verwijderen" has the order.

## Changing the code

```sh
uv sync
uv run pytest                                   # all rules are tested, incl. docs/ONTWERP.md §7.3 example days
uv run ruff check . && uv run ruff format --check .
terraform fmt -check -recursive infra && terraform -chdir=infra validate
./scripts/build_lambda.sh                       # dist/lambda.zip, then plan + apply
```

Add user-visible changes to `[Unreleased]` in `CHANGELOG.md` (Dutch, like the docs).

- Money in integer cents (or `Decimal`), never float; odometer in int km; store UTC, present
  Europe/Amsterdam; a "day" is an Amsterdam calendar day.
- Legs are re-derived every hour; user answers and corrections live in separate records and are
  re-applied (`process.py`), so never store user decisions on a leg itself.
- Web UI: server-rendered Jinja2, no JavaScript, every POST needs the CSRF token and passes the
  Origin check; keep `Referrer-Policy: same-origin` and no `<meta name="referrer">`.
- Layout: `src/kmlog/` (`cardata.py` BMW client, `carlink.py` linking from the web, `derive.py`,
  `classify.py`, `process.py`, `actions.py`, `report/`, `store/` SQLite + DynamoDB, `web/`,
  `aws/` Lambda handlers), `infra/` Terraform, `tests/`.

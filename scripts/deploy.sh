#!/usr/bin/env bash
# Deploy a released version: scripts/deploy.sh vX.Y.Z [--plan | --apply] [--build]
#
# A version is code + infra under one git tag. The script checks out the tag, takes dist/lambda.zip
# from the GitHub Release of that tag (checked against lambda.zip.sha256; --build builds it locally
# instead, e.g. for a release without assets), runs Terraform and checks out the previous branch
# again. Downgrading is the same command with an older tag. Needs a clean working tree and .env.
#
#   (no option)  terraform apply: shows the plan and asks before changing anything
#   --plan       only write and show the plan (infra/tfplan, tag in dist/tfplan.tag); changes nothing
#   --apply      apply exactly that saved plan, without asking (Terraform refuses a stale plan)
set -euo pipefail

die() { echo "deploy: $*" >&2; exit 1; }

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }

main() {
  local tag=${1:-} mode=interactive build=
  shift || true
  for arg in "$@"; do
    case $arg in
      --plan) mode=plan ;;
      --apply) mode=apply ;;
      --build) build=1 ;;
      *) die "unknown option $arg" ;;
    esac
  done
  [[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "usage: scripts/deploy.sh vX.Y.Z [--plan | --apply] [--build]"
  cd "$(dirname "$0")/.."
  [[ -z $(git status --porcelain) ]] || die "working tree not clean (commit or stash first)"
  [[ -f .env ]] || die "no .env with the deploy credentials"
  set -a; . ./.env; set +a
  if [[ $mode == apply ]]; then
    [[ -f infra/tfplan && $(cat dist/tfplan.tag 2>/dev/null) == "$tag" ]] \
      || die "no saved plan for $tag: run scripts/deploy.sh $tag --plan first"
  fi

  git fetch --quiet --tags origin
  git rev-parse -q --verify "refs/tags/$tag^{commit}" >/dev/null || die "tag $tag not found"

  # the Terraform state must belong to the account we are logged in to
  local account bucket
  account=$(aws sts get-caller-identity --query Account --output text)
  bucket=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["backend"]["config"]["bucket"])' \
    infra/.terraform/terraform.tfstate) || die "infra not initialised (terraform init, see README)"
  [[ $bucket == *"-$account-"* ]] || die "state bucket $bucket is not of account $account"

  echo "$mode $tag on AWS account $account (state: $bucket); now on $(git describe --tags --always)"
  if [[ $mode == interactive ]]; then
    read -r -p "Continue? [y/N] " ok
    [[ $ok == [yY] ]] || exit 1
  fi

  local back
  back=$(git symbolic-ref -q --short HEAD || git rev-parse HEAD)
  trap 'git checkout --quiet "'"$back"'" && echo "back on '"$back"'"' EXIT
  git checkout --quiet --detach "$tag"

  if [[ -n $build ]]; then
    ./scripts/build_lambda.sh
  else
    mkdir -p dist
    rm -f dist/lambda.zip dist/lambda.zip.sha256
    gh release download "$tag" --pattern 'lambda.zip*' --dir dist
    (cd dist && sha256 -c lambda.zip.sha256)
  fi

  terraform -chdir=infra init -input=false -backend-config="bucket=$bucket" >/dev/null
  case $mode in
    plan)
      rm -f infra/tfplan dist/tfplan.tag
      terraform -chdir=infra plan -input=false -out=tfplan
      echo "$tag" > dist/tfplan.tag
      echo "read the plan above, then: scripts/deploy.sh $tag --apply"
      return
      ;;
    apply)
      terraform -chdir=infra apply -input=false tfplan
      rm -f infra/tfplan dist/tfplan.tag
      ;;
    interactive) terraform -chdir=infra apply ;;
  esac

  local url
  url=$(terraform -chdir=infra output -raw web_url)
  echo "login page: HTTP $(curl -s -o /dev/null -w '%{http_code}' "${url}login") (expect 200)"
}

# one line, so bash has read all of it before the checkout replaces this file
main "$@"; exit $?

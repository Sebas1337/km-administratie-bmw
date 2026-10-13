#!/usr/bin/env bash
set -euo pipefail

# uv + project dependencies
pip install --user --quiet uv
export PATH="$HOME/.local/bin:$PATH"
uv sync

# local .env for the AWS deploy credentials (gitignored)
[ -f .env ] || cp .env.example .env

echo "post-create klaar: uv run pytest om te testen, zie README.md om te deployen."

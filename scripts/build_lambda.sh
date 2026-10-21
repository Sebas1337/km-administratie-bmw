#!/usr/bin/env bash
# Build dist/lambda.zip for the kmlog Lambdas (python3.12, arm64).
# Contents: the kmlog package + runtime dependencies from uv.lock (openpyxl, requests, ...),
# as manylinux aarch64 wheels. boto3 is provided by the Lambda runtime and not bundled.
# File timestamps are fixed so an unchanged tree gives an identical zip (stable source hash).
# kmlog/_build.py gets the version from `git describe` (a tag such as v0.2.0, or tag-commits-sha);
# KMLOG_VERSION overrides it.
set -euo pipefail
cd "$(dirname "$0")/.."

build=dist/lambda-build
rm -rf "$build" dist/lambda.zip
mkdir -p "$build"

uv export --frozen --no-dev --no-emit-project --no-hashes --format requirements-txt \
  > dist/lambda-requirements.txt
uv pip install --quiet --target "$build" --requirement dist/lambda-requirements.txt \
  --python-platform aarch64-manylinux2014 --python-version 3.12 --only-binary :all:
cp -r src/kmlog "$build/"
version=${KMLOG_VERSION:-$(git describe --tags --always --dirty 2>/dev/null || echo dev)}
printf '# written by scripts/build_lambda.sh\nVERSION = "%s"\n' "$version" > "$build/kmlog/_build.py"
find "$build" -name __pycache__ -type d -prune -exec rm -rf {} +
# bin/: console scripts of dependencies, unused in Lambda; their shebang holds the local python path
rm -rf "$build"/*.dist-info/RECORD "$build/bin"

python3 - "$build" dist/lambda.zip <<'PY'
import os, sys, zipfile
root, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            info = zipfile.ZipInfo(os.path.relpath(path, root), date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(path, "rb") as f:
                z.writestr(info, f.read())
PY
echo "dist/lambda.zip: $(du -h dist/lambda.zip | cut -f1), version $version"

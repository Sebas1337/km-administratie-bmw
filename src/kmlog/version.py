"""Version of the running code. A release build (scripts/build_lambda.sh) writes `_build.py` with
the git tag into the Lambda zip; from the source tree it is the pyproject version + "+dev"."""

from __future__ import annotations

from importlib import metadata

try:
    from ._build import VERSION  # type: ignore[import-not-found]
except ImportError:
    try:
        VERSION = metadata.version("kmlog") + "+dev"
    except metadata.PackageNotFoundError:
        VERSION = "dev"

"""AWS region from the environment (AWS_REGION as in .env, or AWS_DEFAULT_REGION)."""

from __future__ import annotations

import os


def aws_region() -> str | None:
    """AWS_REGION (as in .env) or AWS_DEFAULT_REGION; boto3 itself only reads the latter."""
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or None

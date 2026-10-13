"""Daily notify Lambda (20:00 Europe/Amsterdam): open questions + "gemist?" by mail.

See kmlog/notify.py. Environment: TABLE_NAME, VEHICLE_ID, REPORT_EMAIL, WEB_URL.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .. import notify
from ..store import Store
from . import env, log_event, send_mail


def handler(event, context, *, store: Store | None = None, ses=None) -> dict:
    import boto3

    from ..store.dynamodb import DynamoDbStore

    store = store or DynamoDbStore(env("TABLE_NAME", "kmlog-data"))
    ses = ses or boto3.client("ses")
    address = env("REPORT_EMAIL")
    result = notify.run(
        store,
        env("VEHICLE_ID", "car"),
        datetime.now(UTC),
        lambda subject, body: send_mail(ses, address, subject, body),
        env("WEB_URL", "") or None,
    )
    log_event(**result)
    return result

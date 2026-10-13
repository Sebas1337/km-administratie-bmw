"""Daily notify job: timeouts, one mail per new question, no duplicates."""

import email
from datetime import UTC, date, datetime, timedelta

import boto3

from kmlog import notify
from kmlog.aws import notify as notify_handler
from kmlog.classify import is_workday
from kmlog.model import LegStatus, Settings
from kmlog.store.dynamodb import DynamoDbStore
from kmlog.store.sqlite import SqliteStore
from test_aws_handlers import seeded
from test_voorbeelddagen import WORKDAY, normal_day, observations

EVENING = datetime(2026, 10, 7, 18, 0, tzinfo=UTC)  # 20:00 Amsterdam


def private_day(day):
    return [
        ("00:00", "10:00", "home", 50_000),
        ("10:40", "14:00", "unknown", 50_023),
        ("14:40", "23:00", "home", 50_046),
    ]


def test_mails_new_questions_once_and_times_out():
    store = seeded(SqliteStore())
    store.put_observations(observations(WORKDAY, normal_day(96)))
    mails = []
    send = lambda s, b: mails.append((s, b))  # noqa: E731

    result = notify.run(store, "car", EVENING, send, "https://web.example/")
    assert result == {"event": "notify", "questions": 1, "missed_days": 0, "mailed": True}
    subject, body = mails[0]
    assert subject == "Kilometeradministratie: 1 vraag"
    assert "wo 07-10: Klant → Thuis, 96 km gereden (nu 70 km zakelijk)" in body
    assert "https://web.example/" in body

    assert not notify.run(store, "car", EVENING + timedelta(days=1), send, None)["mailed"]
    assert len(mails) == 1

    notify.run(store, "car", EVENING + timedelta(days=8), send, None)
    legs = store.legs("car", WORKDAY, WORKDAY)
    assert legs[1].status is LegStatus.TIMED_OUT
    assert int(sum(leg.business_km for leg in legs)) == 141
    assert len(mails) == 1


def test_missed_business_day_and_nothing_to_report():
    store = seeded(SqliteStore())
    mails = []
    send = lambda s, b: mails.append((s, b))  # noqa: E731
    assert not notify.run(store, "car", EVENING, send, None)["mailed"]

    store.put_settings(Settings(mail_missed_days=True))
    day = date(2026, 10, 6)  # tuesday: private day of 46 km -> question (rule 5) + rule 7
    store.put_observations(observations(day, private_day(day)))
    result = notify.run(store, "car", EVENING, send, None)
    assert (result["questions"], result["missed_days"]) == (1, 1)
    assert "gemist?" in mails[0][1] and "di 06-10" in mails[0][1]
    assert not notify.run(store, "car", EVENING, send, None)["mailed"]


def test_missed_days_are_not_mailed_by_default():
    store = seeded(SqliteStore())
    mails = []
    send = lambda s, b: mails.append((s, b))  # noqa: E731
    day = date(2026, 10, 6)
    short = [("00:00", "10:00", "home", 50_000), ("10:40", "23:00", "home", 50_016)]
    store.put_observations(observations(day, short))  # 16 km home -> home: no question
    result = notify.run(store, "car", EVENING, send, None)
    assert (result["questions"], result["missed_days"], result["mailed"]) == (0, 0, False)

    store.put_settings(Settings(mail_missed_days=True))  # switched on later: no backlog
    assert not notify.run(store, "car", EVENING, send, None)["mailed"]
    assert mails == []


def test_handler_sends_via_ses(aws, dynamodb_table, monkeypatch):
    monkeypatch.setenv("REPORT_EMAIL", "me@example.com")
    monkeypatch.setenv("WEB_URL", "https://abc.lambda-url.eu-central-1.on.aws/")
    ses = boto3.client("ses")
    ses.verify_email_identity(EmailAddress="me@example.com")
    store = seeded(DynamoDbStore(dynamodb_table))
    now = datetime.now(UTC)
    day = now.date() - timedelta(days=1)
    while not is_workday(day):  # a weekend day would add a weekend question per segment
        day -= timedelta(days=1)
    store.put_observations(observations(day, normal_day(96)))
    result = notify_handler.handler({}, None, store=store)
    assert result["mailed"]
    from moto.backends import get_backend

    sent = get_backend("ses")["123456789012"]["eu-central-1"].sent_messages
    msg = email.message_from_string(sent[-1].raw_data)
    assert msg["Subject"] == "KM Admin - Kilometeradministratie: 1 vraag"
    assert "lambda-url" in msg.get_payload(decode=True).decode()

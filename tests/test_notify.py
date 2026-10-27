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
from test_voorbeelddagen import WORKDAY, normal_day, observations, weekend_gap

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
    assert result == {
        "event": "notify",
        "questions": 1,
        "missed_days": 0,
        "gaps": 0,
        "outage": False,
        "mailed": True,
    }
    subject, body = mails[0]
    assert subject == "Kilometeradministratie: 1 vraag"
    assert "wo 07-10: Klant → Thuis, 96 km gereden (nu 70 km zakelijk)" in body
    assert "https://web.example/" in body

    # the days after have no polls (an outage mail), but the question is not mailed again
    assert notify.run(store, "car", EVENING + timedelta(days=1), send, None)["questions"] == 0

    notify.run(store, "car", EVENING + timedelta(days=8), send, None)
    legs = store.legs("car", WORKDAY, WORKDAY)
    assert legs[1].status is LegStatus.TIMED_OUT
    assert int(sum(leg.business_km for leg in legs)) == 141
    assert [s for s, _ in mails if "vraag" in s] == ["Kilometeradministratie: 1 vraag"]


def test_missed_business_day_and_nothing_to_report():
    store = seeded(SqliteStore())
    mails = []
    send = lambda s, b: mails.append((s, b))  # noqa: E731
    assert not notify.run(store, "car", EVENING, send, None)["mailed"]

    store.put_settings(Settings(mail_missed_days=True))
    day = date(2026, 10, 6)  # tuesday: private day of 46 km -> no question (rule 5), rule 7
    store.put_observations(observations(day, private_day(day)))
    result = notify.run(store, "car", EVENING, send, None)
    assert (result["questions"], result["missed_days"]) == (0, 1)
    assert "gemist?" in mails[0][1] and "di 06-10" in mails[0][1]
    assert not notify.run(store, "car", EVENING, send, None)["mailed"]


def test_missed_days_are_not_mailed_by_default():
    store = seeded(SqliteStore())
    mails = []
    send = lambda s, b: mails.append((s, b))  # noqa: E731
    day = date(2026, 10, 6)
    short = [("00:00", "10:00", "home", 50_000), ("10:40", "23:00", "home", 50_016)]
    store.put_observations(observations(day, short))  # 16 km home -> home: no question
    evening = EVENING - timedelta(days=1)  # that day: no outage either
    result = notify.run(store, "car", evening, send, None)
    assert (result["questions"], result["missed_days"], result["mailed"]) == (0, 0, False)

    store.put_settings(Settings(mail_missed_days=True))  # switched on later: no backlog
    assert not notify.run(store, "car", evening, send, None)["mailed"]
    assert mails == []


FRIDAY = date(2026, 10, 9)
MONDAY = date(2026, 10, 12)


def test_outage_mail_while_bmw_is_down_on_a_workday():
    store = seeded(SqliteStore())
    store.put_observations(observations(FRIDAY, weekend_gap()[:1]))  # last poll Friday 19:00
    mails = []
    send = lambda s, b: mails.append((s, b))  # noqa: E731
    # Friday 20:00: only 20:00 itself is missing so far; Saturday/Sunday: weekend
    for day in (9, 10, 11):
        assert not notify.run(store, "car", datetime(2026, 10, day, 18, tzinfo=UTC), send, None)[
            "mailed"
        ]
    result = notify.run(store, "car", datetime(2026, 10, 12, 18, tzinfo=UTC), send, "https://w/")
    assert result["outage"] and result["mailed"]
    subject, body = mails[0]
    assert subject == "Kilometeradministratie: BMW niet bereikbaar"
    assert "niet bereikbaar sinds vr 09-10 19:00" in body
    assert "https://w/dag/2026-10-12" in body
    # once a day
    assert not notify.run(store, "car", datetime(2026, 10, 12, 19, tzinfo=UTC), send, None)[
        "mailed"
    ]


def test_outage_back_the_same_day_without_km_gives_no_mail():
    store = seeded(SqliteStore())
    # no polls 09:00-13:00 (four missed), the car stayed home
    store.put_observations(
        observations(MONDAY, [("00:00", "08:00", "home", 0), ("13:30", "23:00", "home", 0)])
    )
    result = notify.run(store, "car", datetime(2026, 10, 12, 18, tzinfo=UTC), lambda *a: 0, None)
    assert (result["gaps"], result["outage"], result["mailed"]) == (0, False, False)


def test_gap_mail_once_the_polls_are_back():
    store = seeded(SqliteStore())
    store.put_observations(observations(FRIDAY, weekend_gap()))
    mails = []
    send = lambda s, b: mails.append((s, b))  # noqa: E731
    result = notify.run(store, "car", datetime(2026, 10, 12, 18, tzinfo=UTC), send, "https://w/")
    assert (result["gaps"], result["questions"], result["outage"]) == (1, 0, False)
    subject, body = mails[0]
    assert subject == "Kilometeradministratie: metingen ontbraken"
    assert (
        "- vr 09-10 19:00 tot ma 12-10 09:22: 93 km gereden (Thuis → Klant), "
        "nu 70 km zakelijk op ma 12-10: https://w/dag/2026-10-12"
    ) in body
    assert not notify.run(store, "car", datetime(2026, 10, 12, 19, tzinfo=UTC), send, None)[
        "mailed"
    ]


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

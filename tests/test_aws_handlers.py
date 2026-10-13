import email
import json
from datetime import UTC, date, datetime

import boto3
import pytest

from kmlog import cardata
from kmlog.aws import poller, report
from kmlog.model import LegSource
from kmlog.process import rederive
from kmlog.store.dynamodb import DynamoDbStore
from kmlog.store.sqlite import SqliteStore
from test_cardata import NOW, TELEMATIC, FakeSession, MemoryStore, Resp
from test_maandstaat import CAR, RATE, manual
from test_voorbeelddagen import LOCATIONS, ROUTES, WORKDAY, normal_day, observations


def seeded(store):
    for loc in LOCATIONS:
        store.put_location(loc)
    for r in ROUTES:
        store.put_route(r)
    store.put_vehicle(CAR)
    store.put_rate(RATE)
    return store


def test_rederive_rebuilds_recent_cardata_legs_only():
    store = seeded(SqliteStore())
    store.put_observations(observations(WORKDAY, normal_day(70)))
    store.replace_legs(
        "car", LegSource.MANUAL, WORKDAY, WORKDAY, [manual(WORKDAY, 140)]
    )  # untouched
    end_of_day = datetime(2026, 10, 7, 22, 30, tzinfo=UTC)
    legs = rederive(store, "car", end_of_day)
    assert [int(leg.business_km) for leg in legs] == [71, 70]
    assert len(store.legs("car", WORKDAY, WORKDAY)) == 3
    rederive(store, "car", end_of_day)  # idempotent
    assert len(store.legs("car", WORKDAY, WORKDAY)) == 3


def test_rederive_keeps_night_shift_on_departure_day():
    store = seeded(SqliteStore())
    day = date(2026, 7, 2)
    store.put_observations(
        observations(
            day,
            [
                ("00:00", "20:10", "home", 40_000),
                ("21:10", "32:30", "client", 40_070),
                ("33:30", "40:00", "home", 40_140),
            ],
        )
    )
    legs = rederive(store, "car", datetime(2026, 7, 3, 14, 0, tzinfo=UTC))
    assert [leg.date for leg in legs] == [day, day]


def test_quota_guard_stops_beyond_limit():
    store = SqliteStore()
    guard = poller.quota_guard(store, 2, now=lambda: datetime(2026, 10, 11, 12, tzinfo=UTC))
    guard("GET", "/x")
    guard("GET", "/x")
    with pytest.raises(poller.QuotaExceeded):
        guard("GET", "/x")


def test_poller_handler_end_to_end(aws, dynamodb_table, monkeypatch, capsys):
    sm = boto3.client("secretsmanager")
    sm.create_secret(
        Name="kmlog/cardata",
        SecretString=json.dumps(
            {
                "client_id": "cid",
                "access_token": "at",
                "refresh_token": "rt",
                "expires_at": NOW + 60,  # expires within 5 min -> refresh first
                "vin": "V",
                "container_id": "C",
                "container_descriptors": cardata.DESCRIPTORS,
            }
        ),
    )
    session = FakeSession(
        {
            ("POST", cardata.TOKEN_URL): [
                Resp(200, {"access_token": "at2", "refresh_token": "rt2", "expires_in": 3600})
            ],
            ("GET", "/customers/vehicles/V/telematicData"): [Resp(200, TELEMATIC)],
        }
    )
    monkeypatch.setattr(cardata.requests, "Session", lambda: session)
    monkeypatch.setattr(cardata.time, "time", lambda: NOW)
    store = seeded(DynamoDbStore(dynamodb_table))

    result = poller.handler({}, None, store=store)

    assert result["odometer_km"] == 23456
    assert result["quota_used"] == 1  # the refresh is not an API call
    assert result["new_observation"] is True
    secret = json.loads(sm.get_secret_value(SecretId="kmlog/cardata")["SecretString"])
    assert (secret["access_token"], secret["refresh_token"]) == ("at2", "rt2")
    assert len(store.observations("car")) == 1
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["event"] == "poll"
    assert "lat" not in line and "lon" not in line  # no raw position in the logs


def test_poller_logs_quota_exceeded_instead_of_failing(aws, dynamodb_table, monkeypatch):
    store = seeded(DynamoDbStore(dynamodb_table))
    today = datetime.now(UTC).date()
    for _ in range(40):
        store.increment_quota(today)
    tokens = MemoryStore(
        {
            "client_id": "c",
            "access_token": "a",
            "refresh_token": "r",
            "expires_at": 4e9,
            "vin": "V",
            "container_id": "C",
            "container_descriptors": cardata.DESCRIPTORS,
        }
    )
    monkeypatch.setattr(cardata.requests, "Session", lambda: FakeSession({}))
    result = poller.handler({}, None, store=store, token_store=tokens)
    json.dumps(result)  # Lambda must be able to marshal the return value
    assert result["event"] == "quota_exceeded"


def test_report_handler_uploads_and_mails(aws, dynamodb_table, monkeypatch):
    monkeypatch.setenv("REPORTS_BUCKET", "kmlog-reports-test")
    monkeypatch.setenv("REPORT_EMAIL", "me@example.com")
    s3 = boto3.client("s3")
    s3.create_bucket(
        Bucket="kmlog-reports-test",
        CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
    )
    ses = boto3.client("ses")
    ses.verify_email_identity(EmailAddress="me@example.com")
    store = seeded(DynamoDbStore(dynamodb_table))
    store.replace_legs(
        "car",
        LegSource.MANUAL,
        date(2026, 9, 1),
        date(2026, 9, 30),
        [manual(date(2026, 9, 14), 88, "Leverancier"), manual(date(2026, 9, 16), 140)],
    )

    result = report.handler({"year": 2026, "month": 9}, None, store=store)
    json.dumps(result)  # Lambda must be able to marshal the return value

    assert result["business_km"] == 228
    assert result["amount_cents"] == 5700
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="kmlog-reports-test")["Contents"]]
    assert sorted(keys) == ["reports/2026/2026-09.xlsx", "reports/2026/jaaroverzicht-2026.xlsx"]
    from moto.backends import get_backend

    sent = get_backend("ses")["123456789012"]["eu-central-1"].sent_messages
    msg = email.message_from_string(sent[-1].raw_data)
    assert msg["Subject"] == "KM Admin - Maandstaat september 2026"
    body = msg.get_payload()[0].get_payload(decode=True).decode()
    assert "228 km × € 0,25 = € 57,00" in body
    names = [p.get_filename() for p in msg.get_payload()[1:]]
    assert names == ["2026-09.xlsx"]  # jaaroverzicht only in the last mail of the year

    report.handler({"year": 2026, "month": 9, "jaaroverzicht": True}, None, store=store)
    msg = email.message_from_string(sent[-1].raw_data)
    names = [p.get_filename() for p in msg.get_payload()[1:]]
    assert names == ["2026-09.xlsx", "jaaroverzicht-2026.xlsx"]

    store.replace_legs(
        "car", LegSource.MANUAL, date(2026, 12, 1), date(2026, 12, 31),
        [manual(date(2026, 12, 3), 140)],
    )  # fmt: skip
    assert report.handler({"year": 2026, "month": 12}, None, store=store)["jaaroverzicht_attached"]


def test_previous_month():
    assert report.previous_month(datetime(2026, 11, 1, 5, tzinfo=UTC)) == (2026, 10)
    assert report.previous_month(datetime(2027, 1, 1, 5, tzinfo=UTC)) == (2026, 12)
    # 31 dec 23:30 UTC is already 1 jan in Amsterdam
    assert report.previous_month(datetime(2026, 12, 31, 23, 30, tzinfo=UTC)) == (2026, 12)
    assert report.previous_month(datetime(2026, 12, 31, 22, 0, tzinfo=UTC)) == (2026, 11)

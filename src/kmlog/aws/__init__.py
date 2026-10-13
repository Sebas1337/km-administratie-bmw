"""AWS Lambda handlers. boto3 comes from the Lambda runtime, it is not bundled.

Environment (set by Terraform):
    TABLE_NAME     DynamoDB table (kmlog-data)
    SECRET_ID      Secrets Manager secret with the CarData tokens (kmlog/cardata)
    VEHICLE_ID     key of observations/legs (default "car", same as `kmlog --vehicle`)
    QUOTA_LIMIT    max CarData API calls per UTC day (40; BMW's limit is 50)
    REPORTS_BUCKET S3 bucket for maandstaten (report only)
    REPORT_EMAIL   SES-verified address; sender and recipient of all mail
    WEB_SECRET_ID  Secrets Manager secret with the session signing key (kmlog/web; web only)
    WEB_URL        public URL of the web UI, used in notification mails (notify only)
"""

import json
import logging
import os

log = logging.getLogger("kmlog")
log.setLevel(logging.INFO)


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"environment variable {name} is not set")
    return value


def log_event(**fields) -> None:
    """One structured JSON line per event (CloudWatch Logs Insights friendly)."""
    print(json.dumps(fields, default=str, sort_keys=True))


SUBJECT_PREFIX = "KM Admin"  # fixed prefix so a mail rule can filter every kmlog mail


def mail_subject(subject: str) -> str:
    return subject if subject.startswith(SUBJECT_PREFIX) else f"{SUBJECT_PREFIX} - {subject}"


def send_mail(ses, address: str, subject: str, body: str) -> None:
    """Plain-text mail from and to the one SES-verified address (SES sandbox is fine)."""
    from email.mime.text import MIMEText

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"], msg["To"], msg["Subject"] = address, address, mail_subject(subject)
    ses.send_raw_email(Source=address, Destinations=[address], RawMessage={"Data": msg.as_bytes()})

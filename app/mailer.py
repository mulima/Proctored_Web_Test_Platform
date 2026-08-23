"""Pluggable outbound email.

Three backends, chosen by MAIL_BACKEND:

  console  writes the message to stdout. The default, so the app runs with nothing
           configured and a developer can read verification links out of the log.
  smtp     any SMTP server, including a Gmail app password or a university relay.
  resend   the Resend HTTP API, which needs no SMTP egress - useful because some
           hosts block outbound port 587.

Every course can set its own of these at /{slug}/admin/setup - see resolve() below.
A lecturer who leaves theirs unset falls back to the platform's own MAIL_BACKEND/
SMTP_*/RESEND_API_KEY (app/config.py), which the platform operator controls. That
mirrors the platform's own original default: it runs with nothing configured, on
console, same as a course now does if the lecturer never touches this page.

Sending never raises into a request handler. An exam must not fail to submit because
an inbox is full, so failures are logged and reported to the caller as False.
"""

import json
import smtplib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage

from app.config import settings
from app.tenant_crypto import decrypt


@dataclass
class Attachment:
    filename: str
    content: bytes
    mime_type: str = "application/pdf"

    @property
    def maintype(self) -> str:
        return self.mime_type.split("/")[0]

    @property
    def subtype(self) -> str:
        return self.mime_type.split("/")[-1]


@dataclass
class Message:
    to: str
    subject: str
    body: str
    html: str | None = None
    attachments: list[Attachment] = field(default_factory=list)


class MailError(Exception):
    pass


@dataclass
class MailConfig:
    backend: str
    mail_from: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_use_tls: bool
    resend_api_key: str


def resolve(lecturer=None) -> MailConfig:
    """A course's own email config, falling back field-by-field to the platform's.

    `lecturer` is an app.models_platform.Lecturer, or None for platform-level mail
    (there isn't much of that - mostly this is called with a real lecturer). Secrets
    are decrypted here, in-memory, right before use - never stored or logged in the
    clear anywhere else.
    """
    if lecturer is None:
        return MailConfig(
            backend=(settings.mail_backend or "console").lower(),
            mail_from=settings.mail_from,
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port,
            smtp_username=settings.smtp_username,
            smtp_password=settings.smtp_password,
            smtp_use_tls=settings.smtp_use_tls,
            resend_api_key=settings.resend_api_key,
        )
    smtp_password = ""
    if lecturer.smtp_password_encrypted:
        smtp_password = decrypt(lecturer.smtp_password_encrypted)
    resend_api_key = ""
    if lecturer.resend_api_key_encrypted:
        resend_api_key = decrypt(lecturer.resend_api_key_encrypted)
    return MailConfig(
        backend=(lecturer.mail_backend or settings.mail_backend or "console").lower(),
        mail_from=lecturer.mail_from or settings.mail_from,
        smtp_host=lecturer.smtp_host or settings.smtp_host,
        smtp_port=lecturer.smtp_port or settings.smtp_port,
        smtp_username=lecturer.smtp_username or settings.smtp_username,
        smtp_password=smtp_password or settings.smtp_password,
        smtp_use_tls=lecturer.smtp_use_tls if lecturer.smtp_host else settings.smtp_use_tls,
        resend_api_key=resend_api_key or settings.resend_api_key,
    )


def send(message: Message, lecturer=None) -> bool:
    try:
        config = resolve(lecturer)
        if config.backend == "smtp":
            _send_smtp(message, config)
        elif config.backend == "resend":
            _send_resend(message, config)
        else:
            _send_console(message, config)
        return True
    except Exception as exc:  # never let mail failure break a request
        print(f"[mailer] FAILED to send to {message.to}: {type(exc).__name__}: {exc}", flush=True)
        return False


def _send_console(message: Message, config: MailConfig) -> None:
    attached = ", ".join(a.filename for a in message.attachments) or "none"
    print(
        "\n"
        "==================== EMAIL (console backend) ====================\n"
        f"To:          {message.to}\n"
        f"From:        {config.mail_from}\n"
        f"Subject:     {message.subject}\n"
        f"Attachments: {attached}\n"
        "-----------------------------------------------------------------\n"
        f"{message.body}\n"
        "=================================================================\n",
        flush=True,
    )


def _build_mime(message: Message, config: MailConfig) -> EmailMessage:
    mime = EmailMessage()
    mime["From"] = config.mail_from
    mime["To"] = message.to
    mime["Subject"] = message.subject
    mime.set_content(message.body)
    if message.html:
        mime.add_alternative(message.html, subtype="html")
    for attachment in message.attachments:
        mime.add_attachment(
            attachment.content,
            maintype=attachment.maintype,
            subtype=attachment.subtype,
            filename=attachment.filename,
        )
    return mime


def _send_smtp(message: Message, config: MailConfig) -> None:
    if not config.smtp_host:
        raise MailError("MAIL_BACKEND=smtp but no SMTP_HOST is configured (platform or course).")
    mime = _build_mime(message, config)
    context = ssl.create_default_context()
    if config.smtp_port == 465:
        with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, context=context, timeout=20) as server:
            if config.smtp_username:
                server.login(config.smtp_username, config.smtp_password)
            server.send_message(mime)
        return
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=20) as server:
        server.ehlo()
        if config.smtp_use_tls:
            server.starttls(context=context)
            server.ehlo()
        if config.smtp_username:
            server.login(config.smtp_username, config.smtp_password)
        server.send_message(mime)


def _send_resend(message: Message, config: MailConfig) -> None:
    import base64

    if not config.resend_api_key:
        raise MailError("MAIL_BACKEND=resend but no RESEND_API_KEY is configured (platform or course).")
    payload = {
        "from": config.mail_from,
        "to": [message.to],
        "subject": message.subject,
        "text": message.body,
    }
    if message.html:
        payload["html"] = message.html
    if message.attachments:
        payload["attachments"] = [
            {
                "filename": attachment.filename,
                "content": base64.b64encode(attachment.content).decode("ascii"),
            }
            for attachment in message.attachments
        ]
    request = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.resend_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
    except urllib.error.HTTPError as error:
        raise MailError(f"Resend returned {error.code}: {error.read()[:300]!r}") from error

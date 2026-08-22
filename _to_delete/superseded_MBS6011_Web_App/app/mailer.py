"""Pluggable outbound email.

Three backends, chosen by MAIL_BACKEND:

  console  writes the message to stdout. The default, so the app runs with nothing
           configured and a developer can read verification links out of the log.
  smtp     any SMTP server, including a Gmail app password or a university relay.
  resend   the Resend HTTP API, which needs no SMTP egress - useful because some
           hosts block outbound port 587.

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


def send(message: Message) -> bool:
    backend = (settings.mail_backend or "console").lower()
    try:
        if backend == "smtp":
            _send_smtp(message)
        elif backend == "resend":
            _send_resend(message)
        else:
            _send_console(message)
        return True
    except Exception as exc:  # never let mail failure break a request
        print(f"[mailer] FAILED to send to {message.to}: {type(exc).__name__}: {exc}", flush=True)
        return False


def _send_console(message: Message) -> None:
    attached = ", ".join(a.filename for a in message.attachments) or "none"
    print(
        "\n"
        "==================== EMAIL (console backend) ====================\n"
        f"To:          {message.to}\n"
        f"From:        {settings.mail_from}\n"
        f"Subject:     {message.subject}\n"
        f"Attachments: {attached}\n"
        "-----------------------------------------------------------------\n"
        f"{message.body}\n"
        "=================================================================\n",
        flush=True,
    )


def _build_mime(message: Message) -> EmailMessage:
    mime = EmailMessage()
    mime["From"] = settings.mail_from
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


def _send_smtp(message: Message) -> None:
    if not settings.smtp_host:
        raise MailError("MAIL_BACKEND=smtp but SMTP_HOST is not set.")
    mime = _build_mime(message)
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=20) as server:
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(mime)
        return
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
        server.ehlo()
        if settings.smtp_use_tls:
            server.starttls(context=context)
            server.ehlo()
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(mime)


def _send_resend(message: Message) -> None:
    import base64

    if not settings.resend_api_key:
        raise MailError("MAIL_BACKEND=resend but RESEND_API_KEY is not set.")
    payload = {
        "from": settings.mail_from,
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
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
    except urllib.error.HTTPError as error:
        raise MailError(f"Resend returned {error.code}: {error.read()[:300]!r}") from error

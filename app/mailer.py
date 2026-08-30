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


def resolve(course=None) -> MailConfig:
    """A course's own email config, falling back field-by-field to the platform's.

    `course` is an app.models_platform.Course, or None for platform-level mail
    (there isn't much of that - mostly this is called with a real course). Secrets
    are decrypted here, in-memory, right before use - never stored or logged in the
    clear anywhere else.
    """
    if course is None:
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

    # A course can opt into its own SMTP settings. If it has no SMTP identity fields
    # set and is using the platform default backend, prefer the platform secret values
    # so stale encrypted course secrets do not shadow fresh platform credentials.
    course_uses_own_smtp = bool(
        course.mail_backend == "smtp"
        or course.smtp_host
        or course.smtp_username
        or course.smtp_port
    )

    smtp_password = ""
    if course_uses_own_smtp and course.smtp_password_encrypted:
        smtp_password = decrypt(course.smtp_password_encrypted)

    resend_api_key = ""
    if course.mail_backend == "resend" and course.resend_api_key_encrypted:
        resend_api_key = decrypt(course.resend_api_key_encrypted)

    return MailConfig(
        backend=(course.mail_backend or settings.mail_backend or "console").lower(),
        mail_from=course.mail_from or settings.mail_from,
        smtp_host=course.smtp_host or settings.smtp_host,
        smtp_port=course.smtp_port or settings.smtp_port,
        smtp_username=course.smtp_username or settings.smtp_username,
        smtp_password=smtp_password or settings.smtp_password,
        smtp_use_tls=(course.smtp_use_tls if course_uses_own_smtp else settings.smtp_use_tls),
        resend_api_key=resend_api_key or settings.resend_api_key,
    )


def send(message: Message, course=None) -> bool:
    try:
        config = resolve(course)
        if config.backend == "smtp":
            _send_smtp(message, config)
        elif config.backend == "resend":
            _send_resend(message, config)
        else:
            _send_console(message, config)
        return True
    except Exception as exc:  # never let mail failure break a request
        config = resolve(course)
        print(
            "[mailer] FAILED to send "
            f"to {message.to}: {type(exc).__name__}: {exc} "
            f"(backend={config.backend}, host={config.smtp_host or '-'}, "
            f"port={config.smtp_port}, tls={config.smtp_use_tls}, "
            f"username_set={'yes' if bool(config.smtp_username) else 'no'}, "
            f"password_set={'yes' if bool(config.smtp_password) else 'no'})",
            flush=True,
        )
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
    if not config.smtp_username:
        raise MailError(
            "MAIL_BACKEND=smtp but no SMTP_USERNAME is configured in the effective "
            "settings (course override can shadow platform defaults)."
        )
    if not config.smtp_password:
        raise MailError(
            "MAIL_BACKEND=smtp but no SMTP_PASSWORD is configured in the effective "
            "settings (course override can shadow platform defaults)."
        )
    if config.smtp_port == 587 and not config.smtp_use_tls:
        raise MailError("SMTP port 587 requires STARTTLS (enable Use STARTTLS).")
    mime = _build_mime(message, config)
    context = ssl.create_default_context()
    if config.smtp_port == 465:
        with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, context=context, timeout=20) as server:
            server.login(config.smtp_username, config.smtp_password)
            server.send_message(mime)
        return
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=20) as server:
        server.ehlo()
        if config.smtp_use_tls:
            server.starttls(context=context)
            server.ehlo()
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

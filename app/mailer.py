"""Email delivery module supporting Gmail SMTP and Google Apps Script Web App."""

from __future__ import annotations

import base64
import smtplib
import ssl
from email.message import EmailMessage

import httpx

from app.config import Settings


class MailerError(Exception):
    """Raised when sending a document to Kindle fails."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def _split_mime_type(mime_type: str) -> tuple[str, str]:
    cleaned = (mime_type or "application/octet-stream").strip()
    if "/" in cleaned:
        maintype, subtype = cleaned.split("/", 1)
        if maintype and subtype:
            return maintype, subtype
    return "application", "octet-stream"


def _send_via_smtp(
    file_bytes: bytes,
    filename: str,
    subject: str,
    mime_type: str,
    settings: Settings,
) -> dict[str, str]:
    if not settings.smtp_user or not settings.smtp_password:
        raise MailerError(
            "Konfigurasi SMTP belum lengkap (SMTP_USER / SMTP_PASSWORD kosong).",
            status_code=503,
        )

    maintype, subtype = _split_mime_type(mime_type)

    msg = EmailMessage()
    msg["From"] = settings.smtp_user
    msg["To"] = settings.kindle_email
    msg["Subject"] = subject
    msg.set_content(f"Dokumen '{filename}' dikirim dari Gemini2Kindle Web.")
    msg.add_attachment(
        file_bytes,
        maintype=maintype,
        subtype=subtype,
        filename=filename,
    )

    ssl_context = ssl.create_default_context()

    try:
        if settings.smtp_port == 465:
            with smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=60,
                context=ssl_context,
            ) as server:
                server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60) as server:
                server.ehlo()
                server.starttls(context=ssl_context)
                server.ehlo()
                server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailerError(
            "Autentikasi Gmail SMTP gagal. Pastikan memakai Gmail App Password 16 digit "
            "dan akun pengirim sudah benar.",
            status_code=502,
        ) from exc
    except (TimeoutError, OSError, smtplib.SMTPException) as exc:
        raise MailerError(
            f"Gagal mengirim via SMTP ({settings.smtp_host}:{settings.smtp_port}): {exc}. "
            "Jika hosting memblokir port SMTP, ubah MAIL_MODE=apps_script.",
            status_code=502,
        ) from exc

    return {"mode": "smtp", "to": settings.kindle_email}


def _send_via_apps_script(
    file_bytes: bytes,
    filename: str,
    subject: str,
    mime_type: str,
    settings: Settings,
) -> dict[str, str]:
    if not settings.apps_script_url or not settings.apps_script_token:
        raise MailerError(
            "Konfigurasi Google Apps Script belum lengkap (APPS_SCRIPT_URL / APPS_SCRIPT_TOKEN kosong).",
            status_code=503,
        )

    # Keep key name `epub_base64` for backwards compatibility with existing Apps Script deployments,
    # alongside `mime_type` so updated scripts attach PDF/DOCX/TXT/images with exact MIME types.
    payload = {
        "token": settings.apps_script_token,
        "to": settings.kindle_email,
        "subject": subject,
        "filename": filename,
        "mime_type": mime_type,
        "epub_base64": base64.b64encode(file_bytes).decode("ascii"),
    }

    try:
        # Google Apps Script Web App always responds with a 302 redirect to googleusercontent.com
        with httpx.Client(follow_redirects=True, timeout=90.0) as client:
            resp = client.post(settings.apps_script_url, json=payload)
    except httpx.HTTPError as exc:
        raise MailerError(
            f"Gagal menghubungi Google Apps Script Web App: {exc}",
            status_code=502,
        ) from exc

    if resp.status_code >= 400:
        raise MailerError(
            f"Google Apps Script menolak permintaan (HTTP {resp.status_code}).",
            status_code=502,
        )

    content_type = resp.headers.get("content-type", "").lower()
    if "json" not in content_type:
        snippet = resp.text[:160].strip().replace("\n", " ")
        raise MailerError(
            "Google Apps Script tidak mengembalikan JSON (kemungkinan halaman login HTML). "
            f"Pastikan Web App di-deploy dengan akses 'Anyone'. Cuplikan: {snippet}",
            status_code=502,
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise MailerError("Respons JSON dari Google Apps Script tidak valid.", status_code=502) from exc

    if not data.get("ok"):
        err_msg = data.get("error") or "Google Apps Script mengembalikan status gagal."
        raise MailerError(f"Google Apps Script error: {err_msg}", status_code=502)

    return {"mode": "apps_script", "to": settings.kindle_email}


def send_file_to_kindle(
    file_bytes: bytes,
    filename: str,
    subject: str,
    mime_type: str,
    settings: Settings,
) -> dict[str, str]:
    """Dispatch any Kindle-supported file attachment according to ``MAIL_MODE``."""
    if not settings.kindle_email:
        raise MailerError(
            "Alamat email Kindle (KINDLE_EMAIL) belum dikonfigurasi di server.",
            status_code=503,
        )

    if settings.mail_mode == "apps_script":
        return _send_via_apps_script(file_bytes, filename, subject, mime_type, settings)
    if settings.mail_mode == "smtp":
        return _send_via_smtp(file_bytes, filename, subject, mime_type, settings)

    raise MailerError(f"MAIL_MODE tidak dikenal: {settings.mail_mode}", status_code=500)


def send_epub_to_kindle(
    epub_bytes: bytes,
    filename: str,
    title: str,
    settings: Settings,
) -> dict[str, str]:
    """Dispatch EPUB delivery according to ``MAIL_MODE`` (``smtp`` or ``apps_script``)."""
    return send_file_to_kindle(
        file_bytes=epub_bytes,
        filename=filename,
        subject=title,
        mime_type="application/epub+zip",
        settings=settings,
    )

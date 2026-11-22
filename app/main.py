"""FastAPI entrypoint for Gemini2Kindle Web."""

from __future__ import annotations

import hmac
import logging
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import PROJECT_ROOT, Settings, load_settings
from app.converter import convert_markdown_to_epub
from app.latex_renderer import get_effective_math_engine
from app.mailer import MailerError, send_epub_to_kindle, send_file_to_kindle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

STATIC_DIR = PROJECT_ROOT / "static"

# Official Amazon Send-to-Kindle email attachment formats
KINDLE_ALLOWED_MIME: dict[str, str] = {
    ".epub": "application/epub+zip",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".txt": "text/plain",
    ".rtf": "application/rtf",
    ".html": "text/html",
    ".htm": "text/html",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

app = FastAPI(
    title="Gemini2Kindle Web",
    version="1.1.0",
    description="Konversi Markdown + LaTeX dari Gemini menjadi EPUB 3 ramah e-ink dan kirim dokumen langsung ke Kindle.",
)


class DocumentRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="Judul dokumen EPUB")
    markdown: str = Field(..., min_length=1, description="Teks Markdown hasil tombol Copy dari Gemini")
    author: str | None = Field(default=None, max_length=120, description="Nama penulis opsional")
    auto_detect_latex: bool = Field(
        default=True,
        description="Deteksi otomatis rumus LaTeX tanpa tanda $...$ / $$...$$",
    )


def get_current_settings() -> Settings:
    return load_settings()


def verify_app_pin(
    x_app_pin: str | None = Header(default=None, alias="X-App-Pin"),
    settings: Settings = Depends(get_current_settings),
) -> Settings:
    """Protect all POST endpoints with constant-time X-App-Pin validation."""
    if not settings.app_pin:
        raise HTTPException(
            status_code=503,
            detail="APP_PIN belum diatur di server. Atur environment variable APP_PIN terlebih dahulu.",
        )

    if not x_app_pin or not hmac.compare_digest(x_app_pin.strip(), settings.app_pin):
        raise HTTPException(
            status_code=401,
            detail="PIN keamanan tidak valid. Periksa kembali PIN Anda.",
        )

    return settings


def _validate_markdown_size(markdown_text: str, settings: Settings) -> None:
    if len(markdown_text) > settings.max_markdown_chars:
        raise HTTPException(
            status_code=413,
            detail=f"Konten Markdown terlalu panjang (maksimal {settings.max_markdown_chars:,} karakter).",
        )


def _sanitize_upload_filename(raw_name: str, ext: str) -> str:
    base_name = Path(raw_name or "").name
    stem = Path(base_name).stem
    clean_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", stem)
    clean_stem = re.sub(r"\s+", " ", clean_stem).strip(" .-_")
    if not clean_stem:
        clean_stem = "kindle-document"
    if len(clean_stem) > 90:
        clean_stem = clean_stem[:90].rstrip(" .-_")
    return f"{clean_stem}{ext}"


def _content_disposition_header(filename: str) -> str:
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii").strip() or "document.epub"
    if not ascii_fallback.lower().endswith(".epub"):
        ascii_fallback = f"{ascii_fallback}.epub"
    ascii_fallback = ascii_fallback.replace('"', "")
    utf8_encoded = quote(filename, safe="")
    return f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{utf8_encoded}'


@app.get("/api/health")
async def health_check(settings: Settings = Depends(get_current_settings)) -> dict[str, object]:
    """Health and capability endpoint for SnapDeploy / Render / HF Spaces and PWA status badge."""
    return {
        "status": "ok",
        "math_engine": get_effective_math_engine(settings.math_engine),
        "mail_mode": settings.mail_mode,
        "kindle_configured": settings.is_mail_configured,
        "pin_configured": bool(settings.app_pin),
        "max_upload_mb": settings.max_upload_mb,
    }


@app.post("/api/generate-epub")
async def generate_epub(
    payload: DocumentRequest,
    settings: Settings = Depends(verify_app_pin),
) -> Response:
    """Convert Markdown + LaTeX into an EPUB 3 file for direct download."""
    _validate_markdown_size(payload.markdown, settings)

    try:
        result = await run_in_threadpool(
            convert_markdown_to_epub,
            title=payload.title,
            markdown_text=payload.markdown,
            settings=settings,
            author=payload.author,
            auto_detect_latex=payload.auto_detect_latex,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("EPUB generation failed")
        raise HTTPException(status_code=500, detail=f"Gagal membuat EPUB: {exc}") from exc

    headers = {
        "Content-Disposition": _content_disposition_header(result.filename),
        "X-Math-Count": str(result.math_total),
        "X-Math-Unique": str(result.math_unique_rendered),
        "X-Math-Failed": str(result.math_failed),
        "X-Math-Engine": result.math_engine,
        "X-Filename": quote(result.filename, safe=""),
        "Cache-Control": "no-store",
    }
    return Response(
        content=result.epub_bytes,
        media_type="application/epub+zip",
        headers=headers,
    )


@app.post("/api/send-kindle")
async def send_kindle(
    payload: DocumentRequest,
    settings: Settings = Depends(verify_app_pin),
) -> JSONResponse:
    """Generate EPUB 3 and email it directly to the configured Kindle address."""
    _validate_markdown_size(payload.markdown, settings)

    try:
        result = await run_in_threadpool(
            convert_markdown_to_epub,
            title=payload.title,
            markdown_text=payload.markdown,
            settings=settings,
            author=payload.author,
            auto_detect_latex=payload.auto_detect_latex,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("EPUB generation failed before Kindle send")
        raise HTTPException(status_code=500, detail=f"Gagal membuat EPUB: {exc}") from exc

    try:
        delivery = await run_in_threadpool(
            send_epub_to_kindle,
            epub_bytes=result.epub_bytes,
            filename=result.filename,
            title=payload.title.strip(),
            settings=settings,
        )
    except MailerError as exc:
        logger.warning("Kindle email delivery failed: %s", exc)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error during Kindle delivery")
        raise HTTPException(status_code=502, detail=f"Gagal mengirim email: {exc}") from exc

    return JSONResponse(
        content={
            "status": "sent",
            "filename": result.filename,
            "size_bytes": len(result.epub_bytes),
            "math_total": result.math_total,
            "math_unique_rendered": result.math_unique_rendered,
            "math_failed": result.math_failed,
            "math_engine": result.math_engine,
            "mail_mode": delivery["mode"],
        }
    )


@app.post("/api/send-file")
async def send_file(
    file: UploadFile = File(...),
    convert_pdf: bool = Form(default=False),
    settings: Settings = Depends(verify_app_pin),
) -> JSONResponse:
    """Upload any Amazon Send-to-Kindle supported file and deliver it directly to Kindle."""
    raw_name = file.filename or ""
    ext = Path(raw_name).suffix.lower()

    if ext not in KINDLE_ALLOWED_MIME:
        supported_str = ", ".join(sorted(KINDLE_ALLOWED_MIME.keys()))
        raise HTTPException(
            status_code=400,
            detail=f"Format file '{ext or '(tanpa ekstensi)'}' tidak didukung Send-to-Kindle. "
            f"Format yang didukung: {supported_str}.",
        )

    max_bytes = settings.max_upload_mb * 1024 * 1024
    chunks: list[bytes] = []
    total_size = 0
    chunk_size = 512 * 1024  # 512 KB chunks to protect server RAM

    try:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"Ukuran file melebihi batas maksimal {settings.max_upload_mb} MB "
                    "(batasan lampiran email Kindle).",
                )
            chunks.append(chunk)
    finally:
        await file.close()

    if total_size == 0:
        raise HTTPException(status_code=400, detail="File yang dipilih kosong (0 byte).")

    file_bytes = b"".join(chunks)
    safe_filename = _sanitize_upload_filename(raw_name, ext)
    mime_type = KINDLE_ALLOWED_MIME[ext]

    # Amazon Send-to-Kindle converts PDF to reflowable Kindle format when subject is "convert"
    converted_pdf = ext == ".pdf" and bool(convert_pdf)
    subject = "convert" if converted_pdf else Path(safe_filename).stem

    try:
        delivery = await run_in_threadpool(
            send_file_to_kindle,
            file_bytes=file_bytes,
            filename=safe_filename,
            subject=subject,
            mime_type=mime_type,
            settings=settings,
        )
    except MailerError as exc:
        logger.warning("Direct file Kindle delivery failed: %s", exc)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error during direct file Kindle delivery")
        raise HTTPException(status_code=502, detail=f"Gagal mengirim file ke Kindle: {exc}") from exc

    return JSONResponse(
        content={
            "status": "sent",
            "filename": safe_filename,
            "extension": ext,
            "size_bytes": total_size,
            "converted_pdf": converted_pdf,
            "mail_mode": delivery["mode"],
        }
    )


@app.get("/", include_in_schema=False)
async def serve_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")


@app.get("/manifest.json", include_in_schema=False)
async def serve_manifest() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "manifest.json",
        media_type="application/manifest+json; charset=utf-8",
    )


@app.get("/sw.js", include_in_schema=False)
async def serve_service_worker() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

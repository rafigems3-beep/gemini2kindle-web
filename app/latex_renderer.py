"""LaTeX-to-PNG (300 DPI, transparent background) batch renderer.

Supports two engines controlled by ``MATH_ENGINE``:
- ``mathjax``: Local Node.js batch script using ``mathjax-full`` + ``@resvg/resvg-js``
  for full TeX support (AMS, mhchem, matrices, exact inline baseline metrics).
- ``matplotlib``: Lightweight pure-Python fallback using thread-safe
  ``matplotlib.mathtext`` (``Figure`` + ``FigureCanvasAgg``).
- ``auto`` (default): Uses ``mathjax`` when available and falls back to
  ``matplotlib`` automatically if Node.js is unavailable or a specific
  expression fails to render.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import shutil
import struct
import subprocess
import zlib
from dataclasses import dataclass
from pathlib import Path

from app.config import PROJECT_ROOT, Settings

logger = logging.getLogger(__name__)

MATH_RENDERER_DIR = PROJECT_ROOT / "math_renderer"
MATH_RENDERER_SCRIPT = MATH_RENDERER_DIR / "render.js"
MATH_RENDERER_NODE_MODULES = MATH_RENDERER_DIR / "node_modules" / "mathjax-full"


@dataclass(frozen=True)
class MathItem:
    key: str
    latex: str
    display: bool


@dataclass(frozen=True)
class RenderedMath:
    ok: bool
    png_bytes: bytes | None = None
    width_px: int = 0
    height_px: int = 0
    width_em: float | None = None
    height_em: float | None = None
    valign_em: float | None = None
    engine: str = "none"
    error: str | None = None


def is_mathjax_available() -> bool:
    """Return True if Node.js and the local mathjax-full renderer are ready."""
    return (
        shutil.which("node") is not None
        and MATH_RENDERER_SCRIPT.is_file()
        and MATH_RENDERER_NODE_MODULES.exists()
    )


def get_effective_math_engine(preferred: str) -> str:
    """Resolve the active math engine name for diagnostics and headers."""
    mode = (preferred or "auto").lower()
    if mode == "matplotlib":
        return "matplotlib"
    if mode == "mathjax":
        return "mathjax" if is_mathjax_available() else "mathjax (unavailable)"
    return "mathjax" if is_mathjax_available() else "matplotlib"


def ensure_png_dpi(png_bytes: bytes, dpi: int = 300) -> bytes:
    """Ensure a PNG byte stream declares the requested DPI via a valid pHYs chunk."""
    png_sig = b"\x89PNG\r\n\x1a\n"
    if len(png_bytes) < 33 or not png_bytes.startswith(png_sig):
        return png_bytes

    ppm = round(dpi / 0.0254)
    phys_data = struct.pack(">IIB", ppm, ppm, 1)
    phys_crc = zlib.crc32(b"pHYs" + phys_data) & 0xFFFFFFFF
    phys_chunk = struct.pack(">I", len(phys_data)) + b"pHYs" + phys_data + struct.pack(">I", phys_crc)

    # Walk PNG chunks; replace existing pHYs or insert right after IHDR.
    out = bytearray(png_sig)
    offset = 8
    inserted = False
    n = len(png_bytes)
    while offset + 8 <= n:
        length = struct.unpack(">I", png_bytes[offset : offset + 4])[0]
        chunk_type = png_bytes[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > n:
            return png_bytes  # Malformed PNG; leave untouched

        if chunk_type == b"pHYs":
            if not inserted:
                out.extend(phys_chunk)
                inserted = True
        else:
            out.extend(png_bytes[offset:chunk_end])
            if chunk_type == b"IHDR" and not inserted:
                out.extend(phys_chunk)
                inserted = True
        offset = chunk_end

    return bytes(out)


def _render_batch_mathjax(items: list[MathItem], dpi: int, timeout: int) -> dict[str, RenderedMath]:
    """Invoke the local Node.js MathJax + Resvg batch script once for all formulas."""
    payload = {
        "dpi": dpi,
        "items": [
            {"key": item.key, "latex": item.latex, "display": item.display}
            for item in items
        ],
    }

    proc = subprocess.run(
        ["node", str(MATH_RENDERER_SCRIPT)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        timeout=timeout,
        check=False,
    )

    if proc.returncode != 0:
        stderr_text = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Node MathJax renderer exited with code {proc.returncode}: {stderr_text}")

    data = json.loads(proc.stdout.decode("utf-8"))
    raw_results = data.get("results", {})
    rendered: dict[str, RenderedMath] = {}

    for item in items:
        entry = raw_results.get(item.key)
        if not entry or not entry.get("ok"):
            rendered[item.key] = RenderedMath(
                ok=False,
                engine="mathjax",
                error=(entry or {}).get("error", "Unknown MathJax error"),
            )
            continue

        png_raw = base64.b64decode(entry["png_base64"])
        png_dpi = ensure_png_dpi(png_raw, dpi=dpi)
        rendered[item.key] = RenderedMath(
            ok=True,
            png_bytes=png_dpi,
            width_px=int(entry.get("width_px", 0)),
            height_px=int(entry.get("height_px", 0)),
            width_em=float(entry["width_em"]) if entry.get("width_em") is not None else None,
            height_em=float(entry["height_em"]) if entry.get("height_em") is not None else None,
            valign_em=float(entry["valign_em"]) if entry.get("valign_em") is not None else None,
            engine="mathjax",
        )

    return rendered


def _sanitize_for_matplotlib(latex: str) -> str:
    """Normalize common TeX macros into mathtext-compatible subsets."""
    expr = latex.strip()
    replacements = [
        (r"\displaystyle", ""),
        (r"\textstyle", ""),
        (r"\limits", ""),
        (r"\dfrac", r"\frac"),
        (r"\tfrac", r"\frac"),
        (r"\cfrac", r"\frac"),
        (r"\implies", r"\Rightarrow"),
        (r"\iff", r"\Leftrightarrow"),
        (r"\le", r"\leq"),
        (r"\ge", r"\geq"),
        (r"\to", r"\rightarrow"),
        (r"\land", r"\wedge"),
        (r"\lor", r"\vee"),
        (r"\cdot", r"\cdot"),
        (r"\,", r"\ "),
        (r"\;", r"\ "),
        (r"\:", r"\ "),
        (r"\!", ""),
    ]
    for src, dst in replacements:
        expr = expr.replace(src, dst)

    expr = re.sub(r"\\text\s*\{([^{}]*)\}", r"\\mathrm{\1}", expr)
    expr = re.sub(r"\\textbf\s*\{([^{}]*)\}", r"\\mathbf{\1}", expr)
    expr = re.sub(r"\\textit\s*\{([^{}]*)\}", r"\\mathit{\1}", expr)
    expr = re.sub(r"\\operatorname\s*\{([^{}]*)\}", r"\\mathrm{\1}", expr)
    expr = re.sub(r"\\begin\{aligned\*?\}([\s\S]*?)\\end\{aligned\*?\}", r"\1", expr)
    expr = re.sub(r"\\begin\{align\*?\}([\s\S]*?)\\end\{align\*?\}", r"\1", expr)
    expr = expr.replace("&=", "=").replace("&", " ")
    # Collapse multi-line breaks for mathtext single-line parser
    expr = re.sub(r"\\\\\s*", "; ", expr)
    expr = re.sub(r"\s+", " ", expr).strip()
    return expr


def _estimate_matplotlib_valign_em(latex: str, height_em: float) -> float:
    """Estimate a balanced baseline offset in em for inline matplotlib formulas."""
    lower_indicators = ("_", r"\frac", r"\sum", r"\int", r"\prod", "(", "[", "{", "g", "y", "p", "q", "j")
    if r"\frac" in latex or r"\sum" in latex or r"\int" in latex:
        return round(-0.32 * height_em, 3)
    if any(tok in latex for tok in lower_indicators):
        return round(-0.22 * height_em, 3)
    return round(-0.08 * height_em, 3)


def _render_one_matplotlib(item: MathItem, dpi: int) -> RenderedMath:
    """Render a single LaTeX formula using thread-safe matplotlib.mathtext."""
    try:
        import matplotlib
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from PIL import Image
    except ImportError as exc:
        return RenderedMath(ok=False, engine="matplotlib", error=f"Missing dependency: {exc}")

    expr = _sanitize_for_matplotlib(item.latex)
    if not expr:
        return RenderedMath(ok=False, engine="matplotlib", error="Empty LaTeX expression")

    font_pt = 15.0 if item.display else 13.5
    math_str = f"${expr}$"

    try:
        with matplotlib.rc_context({"mathtext.fontset": "stix", "font.family": "serif"}):
            fig = Figure(figsize=(0.1, 0.1), dpi=dpi)
            fig.patch.set_alpha(0.0)
            canvas = FigureCanvasAgg(fig)

            # Measure exact bounding box first on a temporary renderer
            text_artist = fig.text(0.0, 0.0, math_str, fontsize=font_pt, color="#000000")
            canvas.draw()
            renderer = canvas.get_renderer()
            bbox = text_artist.get_window_extent(renderer=renderer)

            pad_px = max(6, round(dpi * 0.02))
            width_in = max(0.05, (bbox.width + 2 * pad_px) / dpi)
            height_in = max(0.05, (bbox.height + 2 * pad_px) / dpi)

            fig.set_size_inches(width_in, height_in)
            text_artist.set_position((
                (pad_px - bbox.x0) / (width_in * dpi),
                (pad_px - bbox.y0) / (height_in * dpi),
            ))
            canvas.draw()

            raw_buf = io.BytesIO()
            fig.savefig(raw_buf, format="png", dpi=dpi, transparent=True)
            raw_buf.seek(0)

            img = Image.open(raw_buf).convert("RGBA")
            alpha_bbox = img.getbbox()
            if alpha_bbox is None:
                return RenderedMath(ok=False, engine="matplotlib", error="Rendered image is empty")

            crop_pad = max(3, round(dpi * 0.012))
            left = max(0, alpha_bbox[0] - crop_pad)
            top = max(0, alpha_bbox[1] - crop_pad)
            right = min(img.width, alpha_bbox[2] + crop_pad)
            bottom = min(img.height, alpha_bbox[3] + crop_pad)
            cropped = img.crop((left, top, right, bottom))

            out_buf = io.BytesIO()
            cropped.save(out_buf, format="PNG", dpi=(dpi, dpi))
            png_bytes = ensure_png_dpi(out_buf.getvalue(), dpi=dpi)

            px_per_em = (dpi * font_pt) / 72.0
            width_em = round(cropped.width / px_per_em, 3)
            height_em = round(cropped.height / px_per_em, 3)
            valign_em = _estimate_matplotlib_valign_em(item.latex, height_em)

            return RenderedMath(
                ok=True,
                png_bytes=png_bytes,
                width_px=cropped.width,
                height_px=cropped.height,
                width_em=width_em,
                height_em=height_em,
                valign_em=valign_em,
                engine="matplotlib",
            )
    except Exception as exc:  # noqa: BLE001
        return RenderedMath(ok=False, engine="matplotlib", error=str(exc))


def render_math_batch(items: list[MathItem], settings: Settings) -> tuple[dict[str, RenderedMath], str]:
    """Render all unique formulas and return ``(results_by_key, primary_engine_used)``."""
    if not items:
        return {}, get_effective_math_engine(settings.math_engine)

    mode = settings.math_engine.lower()
    dpi = settings.math_dpi
    results: dict[str, RenderedMath] = {}
    primary_engine = "matplotlib"

    use_mathjax = mode in {"auto", "mathjax"} and is_mathjax_available()
    if use_mathjax:
        try:
            results = _render_batch_mathjax(items, dpi=dpi, timeout=settings.math_node_timeout)
            primary_engine = "mathjax"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Node MathJax batch renderer failed: %s", exc)
            if mode == "mathjax":
                for item in items:
                    results[item.key] = RenderedMath(ok=False, engine="mathjax", error=str(exc))
                return results, "mathjax"
            use_mathjax = False

    if not use_mathjax:
        primary_engine = "matplotlib"
        for item in items:
            results[item.key] = _render_one_matplotlib(item, dpi=dpi)
        return results, primary_engine

    # In auto mode, fallback per-formula to matplotlib for any expression MathJax couldn't render
    if mode == "auto":
        for item in items:
            res = results.get(item.key)
            if not res or not res.ok:
                fallback = _render_one_matplotlib(item, dpi=dpi)
                if fallback.ok:
                    results[item.key] = fallback

    return results, primary_engine

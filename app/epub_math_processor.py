"""In-place LaTeX detection and 300 DPI PNG rendering for existing EPUB files."""

from __future__ import annotations

import io
import logging
import posixpath
import re
import zipfile
from dataclasses import dataclass
from urllib.parse import unquote

from lxml import etree

from app.config import Settings
from app.converter import (
    XHTML_NS,
    MathOccurrence,
    extract_latex_from_text_fragment,
)
from app.latex_renderer import MathItem, RenderedMath, render_math_batch

logger = logging.getLogger(__name__)

_TOKEN_SPLIT_RE = re.compile(r"(G2KMATH(?:BLK|INL)\d+TOKEN)")

_SKIP_TAG_LOCAL_NAMES = frozenset({
    "head", "title", "script", "style", "code", "pre", "svg", "math", "textarea", "noscript",
})

_MATH_INJECT_CSS = """/* Injected by Gemini2Kindle Web for rendered 300 DPI LaTeX formulas */
div.g2k-math-block,
span.g2k-math-block-inline {
  display: block;
  text-align: center;
  margin: 0.9em 0;
  padding: 0.1em 0;
  page-break-inside: avoid;
  break-inside: avoid;
}

img.g2k-math-block-img {
  display: inline-block;
  max-width: 100%;
  height: auto;
  vertical-align: middle;
}

img.g2k-math-inline {
  display: inline;
  vertical-align: -0.22em;
  max-width: 100%;
  margin: 0 0.08em;
}
"""


@dataclass(frozen=True)
class EpubLatexResult:
    epub_bytes: bytes
    modified: bool
    chapters_updated: int
    math_total: int
    math_unique_rendered: int
    math_failed: int
    math_engine: str


def _local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    return tag.lower()


def _resolve_zip_path(base_dir: str, rel_href: str) -> str:
    clean_href = unquote(rel_href.split("#", 1)[0].split("?", 1)[0])
    joined = posixpath.join(base_dir, clean_href) if base_dir else clean_href
    return posixpath.normpath(joined).lstrip("/")


def _find_opf_path(zf: zipfile.ZipFile) -> str | None:
    if "META-INF/container.xml" in zf.namelist():
        try:
            container_root = etree.fromstring(zf.read("META-INF/container.xml"))
            for el in container_root.iter():
                if _local_name(el.tag) == "rootfile" and el.get("full-path"):
                    candidate = el.get("full-path", "").lstrip("/")
                    if candidate in zf.namelist():
                        return candidate
        except Exception:  # noqa: BLE001
            pass

    for name in zf.namelist():
        if name.lower().endswith(".opf"):
            return name
    return None


def _scan_and_tokenize_element(
    el: etree._Element,
    register_math,
    auto_detect_latex: bool,
) -> bool:
    """Recursively replace LaTeX inside text/tail nodes of ``el`` with math tokens."""
    if _local_name(el.tag) in _SKIP_TAG_LOCAL_NAMES:
        return False

    changed = False
    if el.text:
        new_text = extract_latex_from_text_fragment(
            el.text,
            register_math=register_math,
            auto_detect_latex=auto_detect_latex,
        )
        if new_text != el.text:
            el.text = new_text
            changed = True

    for child in list(el):
        if _scan_and_tokenize_element(child, register_math, auto_detect_latex):
            changed = True
        if child.tail:
            new_tail = extract_latex_from_text_fragment(
                child.tail,
                register_math=register_math,
                auto_detect_latex=auto_detect_latex,
            )
            if new_tail != child.tail:
                child.tail = new_tail
                changed = True

    return changed


def _make_xhtml_tag(ns: str | None, name: str, attrs: dict[str, str] | None = None) -> etree._Element:
    full_name = f"{{{ns}}}{name}" if ns else name
    el = etree.Element(full_name)
    if attrs:
        for k, v in attrs.items():
            el.set(k, v)
    return el


def _build_math_xml_element(
    ns: str | None,
    occ: MathOccurrence,
    rendered: RenderedMath | None,
    rel_img_href: str | None,
    standalone_block: bool,
) -> etree._Element:
    if not rendered or not rendered.ok or not rel_img_href:
        code_el = _make_xhtml_tag(ns, "code", {"class": "g2k-math-fallback"})
        code_el.text = occ.latex
        if occ.display:
            wrapper_name = "div" if standalone_block else "span"
            wrapper_cls = "g2k-math-block" if standalone_block else "g2k-math-block-inline"
            wrapper = _make_xhtml_tag(ns, wrapper_name, {"class": wrapper_cls})
            wrapper.append(code_el)
            return wrapper
        return code_el

    alt_text = re.sub(r"\s+", " ", occ.latex).strip()[:300] or "formula"

    if occ.display:
        style_parts = ["max-width: 100%;", "height: auto;"]
        if rendered.width_em is not None and rendered.width_em > 0:
            style_parts.insert(0, f"width: {rendered.width_em:.3f}em;")
        img_el = _make_xhtml_tag(
            ns,
            "img",
            {
                "class": "g2k-math-block-img",
                "src": rel_img_href,
                "alt": alt_text,
                "style": " ".join(style_parts),
            },
        )
        wrapper_name = "div" if standalone_block else "span"
        wrapper_cls = "g2k-math-block" if standalone_block else "g2k-math-block-inline"
        wrapper = _make_xhtml_tag(ns, wrapper_name, {"class": wrapper_cls})
        wrapper.append(img_el)
        return wrapper

    style_parts = []
    if rendered.height_em is not None and rendered.height_em > 0:
        style_parts.append(f"height: {rendered.height_em:.3f}em;")
        style_parts.append("width: auto;")
    if rendered.valign_em is not None:
        style_parts.append(f"vertical-align: {rendered.valign_em:.3f}em;")

    attrs = {
        "class": "g2k-math-inline",
        "src": rel_img_href,
        "alt": alt_text,
    }
    if style_parts:
        attrs["style"] = " ".join(style_parts)
    return _make_xhtml_tag(ns, "img", attrs)


def _replace_tokens_in_element(
    el: etree._Element,
    ns: str | None,
    occurrences: dict[str, MathOccurrence],
    rendered_by_key: dict[str, RenderedMath],
    rel_href_by_key: dict[str, str],
) -> None:
    """Replace math tokens in ``el`` and its children with rendered <img> elements."""
    if _local_name(el.tag) in _SKIP_TAG_LOCAL_NAMES:
        return

    # Standalone <p>G2KMATHBLK...TOKEN</p> -> replace whole <p> with <div class="g2k-math-block">
    for child in list(el):
        if _local_name(child.tag) == "p" and len(child) == 0 and child.text:
            candidate = child.text.strip()
            occ = occurrences.get(candidate)
            if occ is not None and occ.display:
                block_el = _build_math_xml_element(
                    ns=ns,
                    occ=occ,
                    rendered=rendered_by_key.get(occ.dedup_key),
                    rel_img_href=rel_href_by_key.get(occ.dedup_key),
                    standalone_block=True,
                )
                block_el.tail = child.tail
                idx = el.index(child)
                el.remove(child)
                el.insert(idx, block_el)
                continue

    # Replace tokens inside el.text
    if el.text and _TOKEN_SPLIT_RE.search(el.text):
        parts = _TOKEN_SPLIT_RE.split(el.text)
        el.text = parts[0]
        insert_pos = 0
        for part in parts[1:]:
            if not part:
                continue
            occ = occurrences.get(part)
            if occ is not None:
                math_el = _build_math_xml_element(
                    ns=ns,
                    occ=occ,
                    rendered=rendered_by_key.get(occ.dedup_key),
                    rel_img_href=rel_href_by_key.get(occ.dedup_key),
                    standalone_block=False,
                )
                el.insert(insert_pos, math_el)
                insert_pos += 1
            else:
                if insert_pos > 0:
                    prev = el[insert_pos - 1]
                    prev.tail = (prev.tail or "") + part
                else:
                    el.text = (el.text or "") + part

    # Recurse into children and replace tokens inside each child.tail
    idx = 0
    while idx < len(el):
        child = el[idx]
        _replace_tokens_in_element(child, ns, occurrences, rendered_by_key, rel_href_by_key)

        if child.tail and _TOKEN_SPLIT_RE.search(child.tail):
            parts = _TOKEN_SPLIT_RE.split(child.tail)
            child.tail = parts[0]
            insert_pos = idx + 1
            for part in parts[1:]:
                if not part:
                    continue
                occ = occurrences.get(part)
                if occ is not None:
                    math_el = _build_math_xml_element(
                        ns=ns,
                        occ=occ,
                        rendered=rendered_by_key.get(occ.dedup_key),
                        rel_img_href=rel_href_by_key.get(occ.dedup_key),
                        standalone_block=False,
                    )
                    el.insert(insert_pos, math_el)
                    insert_pos += 1
                else:
                    prev = el[insert_pos - 1]
                    prev.tail = (prev.tail or "") + part
            idx = insert_pos
        else:
            idx += 1


def _inject_css_link(root: etree._Element, ns: str | None, rel_css_href: str) -> None:
    head = None
    for child in root:
        if _local_name(child.tag) == "head":
            head = child
            break
    if head is None:
        return

    for link in head:
        if _local_name(link.tag) == "link" and link.get("href") == rel_css_href:
            return

    link_el = _make_xhtml_tag(
        ns,
        "link",
        {
            "rel": "stylesheet",
            "type": "text/css",
            "href": rel_css_href,
        },
    )
    head.append(link_el)


def process_epub_latex(
    epub_bytes: bytes,
    settings: Settings,
    auto_detect_latex: bool = True,
) -> EpubLatexResult:
    """Detect and render raw LaTeX inside an existing EPUB into 300 DPI transparent PNGs."""
    if not zipfile.is_zipfile(io.BytesIO(epub_bytes)):
        raise ValueError("File EPUB tidak valid atau rusak.")

    with zipfile.ZipFile(io.BytesIO(epub_bytes), "r") as zf:
        opf_path = _find_opf_path(zf)
        if not opf_path:
            raise ValueError("Struktur OPF tidak ditemukan di dalam file EPUB.")

        opf_dir = posixpath.dirname(opf_path)
        opf_bytes = zf.read(opf_path)
        xml_parser = etree.XMLParser(resolve_entities=False, recover=True)
        opf_root = etree.fromstring(opf_bytes, parser=xml_parser)

        manifest_el = None
        for el in opf_root.iter():
            if _local_name(el.tag) == "manifest":
                manifest_el = el
                break
        if manifest_el is None:
            raise ValueError("Bagian manifest OPF tidak ditemukan di dalam file EPUB.")

        existing_ids: set[str] = set()
        chapter_paths: list[str] = []
        for item in manifest_el:
            item_id = item.get("id")
            if item_id:
                existing_ids.add(item_id)
            media_type = (item.get("media-type") or "").lower()
            props = (item.get("properties") or "").split()
            href = item.get("href") or ""
            if media_type in {"application/xhtml+xml", "text/html"} and "nav" not in props and href:
                full_ch_path = _resolve_zip_path(opf_dir, href)
                if full_ch_path in zf.namelist() and full_ch_path not in chapter_paths:
                    chapter_paths.append(full_ch_path)

        occurrences: dict[str, MathOccurrence] = {}
        dedup_map: dict[tuple[bool, str], str] = {}
        unique_items: list[MathItem] = []

        def register_math(raw_latex: str, display: bool) -> str:
            cleaned = raw_latex.strip()
            sig = (display, cleaned)
            if sig not in dedup_map:
                key = f"m{len(unique_items)}"
                dedup_map[sig] = key
                unique_items.append(MathItem(key=key, latex=cleaned, display=display))
            else:
                key = dedup_map[sig]

            prefix = "G2KMATHBLK" if display else "G2KMATHINL"
            token = f"{prefix}{len(occurrences)}TOKEN"
            occurrences[token] = MathOccurrence(
                token=token,
                dedup_key=key,
                latex=cleaned,
                display=display,
            )
            return token

        parsed_chapters: dict[str, tuple[etree._Element, str | None]] = {}
        for ch_path in chapter_paths:
            raw_xhtml = zf.read(ch_path)
            try:
                ch_root = etree.fromstring(raw_xhtml, parser=xml_parser)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping unparseable chapter %s: %s", ch_path, exc)
                continue

            ns = None
            if isinstance(ch_root.tag, str) and ch_root.tag.startswith("{"):
                ns = ch_root.tag[1:].split("}", 1)[0]

            body_el = None
            for child in ch_root:
                if _local_name(child.tag) == "body":
                    body_el = child
                    break
            target_el = body_el if body_el is not None else ch_root

            if _scan_and_tokenize_element(target_el, register_math, auto_detect_latex):
                parsed_chapters[ch_path] = (ch_root, ns)

        # If no LaTeX found anywhere, return original EPUB untouched
        if not unique_items or not parsed_chapters:
            return EpubLatexResult(
                epub_bytes=epub_bytes,
                modified=False,
                chapters_updated=0,
                math_total=0,
                math_unique_rendered=0,
                math_failed=0,
                math_engine="none",
            )

        # Batch render all unique formulas across all chapters at once
        rendered_by_key, engine_used = render_math_batch(unique_items, settings=settings)

        # Pick collision-free asset directory inside OPF folder
        asset_subdir = "g2k_math"
        existing_names = set(zf.namelist())
        suffix_num = 1
        while any(
            n.startswith(_resolve_zip_path(opf_dir, f"{asset_subdir}/"))
            for n in existing_names
        ):
            suffix_num += 1
            asset_subdir = f"g2k_math_{suffix_num}"

        def _unique_manifest_id(base: str) -> str:
            cand = base
            counter = 1
            while cand in existing_ids:
                counter += 1
                cand = f"{base}_{counter}"
            existing_ids.add(cand)
            return cand

        opf_ns = None
        if isinstance(opf_root.tag, str) and opf_root.tag.startswith("{"):
            opf_ns = opf_root.tag[1:].split("}", 1)[0]

        new_zip_files: dict[str, bytes] = {}
        full_path_by_key: dict[str, str] = {}
        unique_ok = 0
        unique_failed = 0

        for idx, item in enumerate(unique_items, start=1):
            res = rendered_by_key.get(item.key)
            if res and res.ok and res.png_bytes:
                rel_from_opf = f"{asset_subdir}/math_{idx:04d}.png"
                zip_entry_path = _resolve_zip_path(opf_dir, rel_from_opf)
                new_zip_files[zip_entry_path] = res.png_bytes
                full_path_by_key[item.key] = zip_entry_path
                unique_ok += 1

                manifest_item = _make_xhtml_tag(
                    opf_ns,
                    "item",
                    {
                        "id": _unique_manifest_id(f"g2k_math_img_{idx:04d}"),
                        "href": rel_from_opf,
                        "media-type": "image/png",
                    },
                )
                manifest_el.append(manifest_item)
            else:
                unique_failed += 1

        css_rel_from_opf = f"{asset_subdir}/g2k_math.css"
        css_zip_path = _resolve_zip_path(opf_dir, css_rel_from_opf)
        new_zip_files[css_zip_path] = _MATH_INJECT_CSS.encode("utf-8")
        manifest_el.append(
            _make_xhtml_tag(
                opf_ns,
                "item",
                {
                    "id": _unique_manifest_id("g2k_math_css"),
                    "href": css_rel_from_opf,
                    "media-type": "text/css",
                },
            )
        )

        # Rewrite modified chapters with relative image & CSS links
        updated_chapters_bytes: dict[str, bytes] = {}
        for ch_path, (ch_root, ns) in parsed_chapters.items():
            ch_dir = posixpath.dirname(ch_path)
            rel_href_by_key = {
                k: posixpath.relpath(full_p, ch_dir or ".")
                for k, full_p in full_path_by_key.items()
            }
            rel_css_href = posixpath.relpath(css_zip_path, ch_dir or ".")

            body_el = None
            for child in ch_root:
                if _local_name(child.tag) == "body":
                    body_el = child
                    break
            target_el = body_el if body_el is not None else ch_root

            _replace_tokens_in_element(
                target_el,
                ns=ns,
                occurrences=occurrences,
                rendered_by_key=rendered_by_key,
                rel_href_by_key=rel_href_by_key,
            )
            _inject_css_link(ch_root, ns=ns, rel_css_href=rel_css_href)

            serialized = etree.tostring(
                ch_root,
                xml_declaration=True,
                encoding="UTF-8",
                method="xml",
            )
            etree.fromstring(serialized)
            updated_chapters_bytes[ch_path] = serialized

        updated_opf_bytes = etree.tostring(
            opf_root,
            xml_declaration=True,
            encoding="UTF-8",
            method="xml",
        )

        # Repack valid EPUB archive (mimetype MUST be first and uncompressed)
        out_buf = io.BytesIO()
        with zipfile.ZipFile(out_buf, "w") as out_zf:
            out_zf.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)

            for info in zf.infolist():
                name = info.filename
                if name == "mimetype":
                    continue
                if name == opf_path:
                    out_zf.writestr(name, updated_opf_bytes, compress_type=zipfile.ZIP_DEFLATED)
                elif name in updated_chapters_bytes:
                    out_zf.writestr(name, updated_chapters_bytes[name], compress_type=zipfile.ZIP_DEFLATED)
                else:
                    out_zf.writestr(name, zf.read(name), compress_type=zipfile.ZIP_DEFLATED)

            for new_name, new_data in new_zip_files.items():
                out_zf.writestr(new_name, new_data, compress_type=zipfile.ZIP_DEFLATED)

    return EpubLatexResult(
        epub_bytes=out_buf.getvalue(),
        modified=True,
        chapters_updated=len(updated_chapters_bytes),
        math_total=len(occurrences),
        math_unique_rendered=unique_ok,
        math_failed=unique_failed,
        math_engine=engine_used,
    )

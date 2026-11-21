"""Markdown + LaTeX -> strict XHTML -> valid EPUB 3 conversion pipeline."""

from __future__ import annotations

import logging
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag
from ebooklib import epub
from lxml import etree
from markdown_it import MarkdownIt

from app.config import Settings
from app.latex_renderer import MathItem, RenderedMath, render_math_batch

logger = logging.getLogger(__name__)

STYLES_DIR = Path(__file__).resolve().parent / "styles"
EINK_CSS_PATH = STYLES_DIR / "eink.css"
XHTML_NS = "http://www.w3.org/1999/xhtml"

# Strip illegal XML 1.0 control characters (keep tab \x09, LF \x0a, CR \x0d)
_ILLEGAL_XML_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_DISPLAY_BRACKET_RE = re.compile(r"(?<!\\)\\\[([\s\S]+?)\\\]")
_DISPLAY_DOLLAR_RE = re.compile(r"(?<!\\)\$\$([\s\S]+?)\$\$")
_INLINE_PAREN_RE = re.compile(r"(?<!\\)\\\(((?:(?!\n\n)[\s\S])+?)\\\)")
_MATH_TOKEN_RE = re.compile(r"(G2KMATH(?:BLK|INL)\d+TOKEN)")

_VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link", "col", "area", "base", "embed", "param", "source", "track", "wbr"})


@dataclass(frozen=True)
class MathOccurrence:
    token: str
    dedup_key: str
    latex: str
    display: bool


@dataclass(frozen=True)
class ConversionResult:
    epub_bytes: bytes
    filename: str
    math_total: int
    math_unique_rendered: int
    math_failed: int
    math_engine: str


def sanitize_filename(title: str) -> str:
    """Create a safe .epub filename from a document title."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", title or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-_")
    if not cleaned:
        cleaned = "gemini-document"
    if len(cleaned) > 80:
        cleaned = cleaned[:80].rstrip(" .-_")
    if not cleaned.lower().endswith(".epub"):
        cleaned = f"{cleaned}.epub"
    return cleaned


def _count_preceding_backslashes(text: str, pos: int) -> int:
    count = 0
    idx = pos - 1
    while idx >= 0 and text[idx] == "\\":
        count += 1
        idx -= 1
    return count


def _protect_fenced_code(markdown_text: str, code_store: dict[str, str]) -> str:
    """Replace CommonMark fenced code blocks (``` or ~~~) with safe alphanumeric tokens."""
    lines = markdown_text.split("\n")
    out_lines: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        open_match = re.match(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$", line)
        if open_match:
            fence = open_match.group(1)
            fence_char = fence[0]
            fence_len = len(fence)
            info_str = open_match.group(2)
            # CommonMark: backtick info string cannot contain backticks
            if fence_char == "`" and "`" in info_str:
                out_lines.append(line)
                i += 1
                continue

            block_lines = [line]
            i += 1
            close_pattern = re.compile(rf"^[ ]{{0,3}}{re.escape(fence_char)}{{{fence_len},}}[ \t]*$")
            while i < n:
                block_lines.append(lines[i])
                if close_pattern.match(lines[i]):
                    i += 1
                    break
                i += 1

            token = f"G2KCODE{len(code_store)}TOKEN"
            code_store[token] = "\n".join(block_lines)
            out_lines.append(token)
        else:
            out_lines.append(line)
            i += 1

    return "\n".join(out_lines)


def _protect_inline_code(text: str, code_store: dict[str, str]) -> str:
    """Replace CommonMark inline backtick code spans with safe alphanumeric tokens."""
    out: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        if text[i] == "`" and _count_preceding_backslashes(text, i) % 2 == 0:
            tick_end = i
            while tick_end < n and text[tick_end] == "`":
                tick_end += 1
            tick_len = tick_end - i

            # Find matching closing run of exact same backtick length within same paragraph
            j = tick_end
            found_end = -1
            while j < n:
                if text[j] == "\n" and j + 1 < n and text[j + 1] == "\n":
                    break
                if text[j] == "`":
                    close_end = j
                    while close_end < n and text[close_end] == "`":
                        close_end += 1
                    if close_end - j == tick_len:
                        found_end = close_end
                        break
                    j = close_end
                else:
                    j += 1

            if found_end != -1:
                token = f"G2KCODE{len(code_store)}TOKEN"
                code_store[token] = text[i:found_end]
                out.append(token)
                i = found_end
                continue

            out.append(text[i:tick_end])
            i = tick_end
        else:
            out.append(text[i])
            i += 1

    return "".join(out)


def _extract_inline_dollar_math(
    text: str,
    register_math,
) -> str:
    """Extract Pandoc-style inline ``$...$`` math without misclassifying currency or escaped ``\\$``."""
    out: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        if text[i] == "$":
            # Escaped \$ or $$ -> leave alone
            if _count_preceding_backslashes(text, i) % 2 == 1:
                out.append("$")
                i += 1
                continue
            if i + 1 >= n or text[i + 1] == "$" or text[i + 1].isspace():
                out.append("$")
                i += 1
                continue

            j = i + 1
            matched = False
            while j < n:
                ch = text[j]
                if ch == "\n" and j + 1 < n and text[j + 1] == "\n":
                    break
                if ch == "\\" and j + 1 < n:
                    j += 2
                    continue
                if ch == "$":
                    prev_ch = text[j - 1]
                    next_ch = text[j + 1] if j + 1 < n else ""
                    if not prev_ch.isspace() and not next_ch.isdigit() and next_ch != "$":
                        inner = text[i + 1 : j].strip()
                        if inner:
                            token = register_math(inner, display=False)
                            out.append(token)
                            i = j + 1
                            matched = True
                            break
                    break
                j += 1

            if not matched:
                out.append("$")
                i += 1
        else:
            out.append(text[i])
            i += 1

    return "".join(out)


def extract_latex_expressions(
    markdown_text: str,
) -> tuple[str, dict[str, MathOccurrence], list[MathItem]]:
    """Extract display and inline LaTeX safely before Markdown parsing.

    Returns:
        - ``protected_markdown``: Markdown with code restored and LaTeX replaced by safe tokens.
        - ``occurrences``: Mapping ``token -> MathOccurrence``.
        - ``unique_items``: Deduplicated list of ``MathItem`` ready for batch rendering.
    """
    normalized = markdown_text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _ILLEGAL_XML_CHARS_RE.sub("", normalized)

    code_store: dict[str, str] = {}
    working = _protect_fenced_code(normalized, code_store)
    working = _protect_inline_code(working, code_store)

    occurrences: dict[str, MathOccurrence] = {}
    dedup_map: dict[tuple[bool, str], str] = {}
    unique_items: list[MathItem] = []

    def register_math(raw_latex: str, display: bool) -> str:
        cleaned = raw_latex.strip()
        dedup_sig = (display, cleaned)
        if dedup_sig not in dedup_map:
            dedup_key = f"m{len(unique_items)}"
            dedup_map[dedup_sig] = dedup_key
            unique_items.append(MathItem(key=dedup_key, latex=cleaned, display=display))
        else:
            dedup_key = dedup_map[dedup_sig]

        prefix = "G2KMATHBLK" if display else "G2KMATHINL"
        token = f"{prefix}{len(occurrences)}TOKEN"
        occurrences[token] = MathOccurrence(
            token=token,
            dedup_key=dedup_key,
            latex=cleaned,
            display=display,
        )
        return token

    def _replace_display(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner:
            return match.group(0)
        return register_math(inner, display=True)

    def _replace_inline_paren(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner:
            return match.group(0)
        return register_math(inner, display=False)

    # 1) Display math first so $$...$$ and \[...\] are never split by inline rules
    working = _DISPLAY_BRACKET_RE.sub(_replace_display, working)
    working = _DISPLAY_DOLLAR_RE.sub(_replace_display, working)

    # 2) Inline math \(...\) and $...$
    working = _INLINE_PAREN_RE.sub(_replace_inline_paren, working)
    working = _extract_inline_dollar_math(working, register_math)

    # 3) Restore protected code blocks & inline code (reverse order handles nested restores safely)
    for token in reversed(list(code_store.keys())):
        working = working.replace(token, code_store[token])

    return working, occurrences, unique_items


def _build_math_tag(
    soup: BeautifulSoup,
    occ: MathOccurrence,
    rendered: RenderedMath | None,
    image_href: str | None,
    standalone_block: bool,
) -> Tag:
    """Create the appropriate XHTML tag for a rendered (or fallback) formula."""
    if not rendered or not rendered.ok or not image_href:
        code_tag = soup.new_tag("code", attrs={"class": "math-fallback"})
        code_tag.string = occ.latex
        if occ.display:
            wrapper_name = "div" if standalone_block else "span"
            wrapper_cls = "math-block" if standalone_block else "math-block-inline"
            wrapper = soup.new_tag(wrapper_name, attrs={"class": wrapper_cls})
            wrapper.append(code_tag)
            return wrapper
        return code_tag

    alt_text = re.sub(r"\s+", " ", occ.latex).strip()[:300] or "formula"

    if occ.display:
        img_style_parts = ["max-width: 100%;", "height: auto;"]
        if rendered.width_em is not None and rendered.width_em > 0:
            # Keep natural typographic width for short equations while clamping wide ones to 100%
            img_style_parts.insert(0, f"width: {rendered.width_em:.3f}em;")
        img = soup.new_tag(
            "img",
            attrs={
                "class": "math-block-img",
                "src": image_href,
                "alt": alt_text,
                "style": " ".join(img_style_parts),
            },
        )
        wrapper_name = "div" if standalone_block else "span"
        wrapper_cls = "math-block" if standalone_block else "math-block-inline"
        wrapper = soup.new_tag(wrapper_name, attrs={"class": wrapper_cls})
        wrapper.append(img)
        return wrapper

    style_parts: list[str] = []
    if rendered.height_em is not None and rendered.height_em > 0:
        style_parts.append(f"height: {rendered.height_em:.3f}em;")
        style_parts.append("width: auto;")
    if rendered.valign_em is not None:
        style_parts.append(f"vertical-align: {rendered.valign_em:.3f}em;")

    attrs = {
        "class": "math-inline",
        "src": image_href,
        "alt": alt_text,
    }
    if style_parts:
        attrs["style"] = " ".join(style_parts)

    return soup.new_tag("img", attrs=attrs)


def _inject_math_and_toc(
    html_fragment: str,
    occurrences: dict[str, MathOccurrence],
    rendered_by_key: dict[str, RenderedMath],
    href_by_key: dict[str, str],
) -> tuple[BeautifulSoup, list[tuple[int, str, str]]]:
    """Replace math tokens in the parsed HTML tree and collect heading anchors for TOC."""
    soup = BeautifulSoup(html_fragment, "html.parser")

    # 1) Replace standalone <p>G2KMATHBLK...TOKEN</p> with <div class="math-block">...</div>
    for p_tag in list(soup.find_all("p")):
        if len(p_tag.contents) == 1 and isinstance(p_tag.contents[0], NavigableString):
            candidate = str(p_tag.contents[0]).strip()
            if candidate in occurrences and occurrences[candidate].display:
                occ = occurrences[candidate]
                rendered = rendered_by_key.get(occ.dedup_key)
                href = href_by_key.get(occ.dedup_key)
                block_tag = _build_math_tag(soup, occ, rendered, href, standalone_block=True)
                p_tag.replace_with(block_tag)

    # 2) Replace all remaining math tokens inside text nodes (skipping <code>/<pre>)
    for text_node in list(soup.find_all(string=_MATH_TOKEN_RE)):
        if text_node.parent and text_node.parent.name in {"code", "pre"}:
            continue
        raw = str(text_node)
        parts = _MATH_TOKEN_RE.split(raw)
        if len(parts) <= 1:
            continue

        new_nodes: list[NavigableString | Tag] = []
        for part in parts:
            if not part:
                continue
            occ = occurrences.get(part)
            if occ is not None:
                rendered = rendered_by_key.get(occ.dedup_key)
                href = href_by_key.get(occ.dedup_key)
                new_nodes.append(_build_math_tag(soup, occ, rendered, href, standalone_block=False))
            else:
                new_nodes.append(NavigableString(part))

        if new_nodes:
            first = new_nodes[0]
            text_node.replace_with(first)
            cursor = first
            for nxt in new_nodes[1:]:
                cursor.insert_after(nxt)
                cursor = nxt

    # 3) Assign deterministic IDs to headings h1-h3 for Kindle TOC navigation
    toc_entries: list[tuple[int, str, str]] = []
    heading_counter = 0
    for heading in soup.find_all(["h1", "h2", "h3"]):
        parts_txt: list[str] = []
        for desc in heading.descendants:
            if isinstance(desc, NavigableString):
                parts_txt.append(str(desc))
            elif isinstance(desc, Tag) and desc.name == "img" and desc.get("alt"):
                parts_txt.append(str(desc["alt"]))
        text_label = re.sub(r"\s+", " ", " ".join(parts_txt)).strip()
        if not text_label:
            continue
        heading_counter += 1
        anchor_id = f"sec-{heading_counter}"
        heading["id"] = anchor_id
        level = int(heading.name[1])
        toc_entries.append((level, text_label, anchor_id))

    return soup, toc_entries


def _append_bs4_children_to_xml(parent_xml: etree._Element, bs4_node: Tag | BeautifulSoup) -> None:
    """Recursively convert BeautifulSoup nodes into strict XHTML lxml elements."""
    for child in bs4_node.children:
        if isinstance(child, NavigableString):
            text = _ILLEGAL_XML_CHARS_RE.sub("", str(child))
            if not text:
                continue
            if len(parent_xml) == 0:
                parent_xml.text = (parent_xml.text or "") + text
            else:
                last_el = parent_xml[-1]
                last_el.tail = (last_el.tail or "") + text
        elif isinstance(child, Tag):
            tag_name = (child.name or "").lower()
            if not re.fullmatch(r"[a-z][a-z0-9:-]*", tag_name):
                continue

            el = etree.SubElement(parent_xml, f"{{{XHTML_NS}}}{tag_name}")
            for attr_k, attr_v in child.attrs.items():
                if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9._:-]*", str(attr_k)):
                    continue
                if isinstance(attr_v, list):
                    val_str = " ".join(str(v) for v in attr_v)
                elif attr_v is True:
                    val_str = str(attr_k)
                else:
                    val_str = str(attr_v)
                el.set(str(attr_k), _ILLEGAL_XML_CHARS_RE.sub("", val_str))

            if tag_name not in _VOID_TAGS:
                _append_bs4_children_to_xml(el, child)


def _build_strict_xhtml(
    title: str,
    author: str,
    language: str,
    soup_body: BeautifulSoup,
) -> bytes:
    """Build and XML-validate a complete EPUB 3 XHTML 1.1/HTML5 XML document."""
    nsmap = {None: XHTML_NS}
    html_el = etree.Element(f"{{{XHTML_NS}}}html", nsmap=nsmap)
    html_el.set("{http://www.w3.org/XML/1998/namespace}lang", language)
    html_el.set("lang", language)

    head_el = etree.SubElement(html_el, f"{{{XHTML_NS}}}head")
    meta_el = etree.SubElement(head_el, f"{{{XHTML_NS}}}meta")
    meta_el.set("charset", "UTF-8")

    title_el = etree.SubElement(head_el, f"{{{XHTML_NS}}}title")
    title_el.text = _ILLEGAL_XML_CHARS_RE.sub("", title)

    body_el = etree.SubElement(html_el, f"{{{XHTML_NS}}}body")

    # Add a clean document header unless the Markdown already starts with a matching H1
    first_tag = next((c for c in soup_body.children if isinstance(c, Tag)), None)
    starts_with_same_h1 = (
        first_tag is not None
        and first_tag.name == "h1"
        and re.sub(r"\s+", " ", first_tag.get_text(" ", strip=True)).strip().lower()
        == re.sub(r"\s+", " ", title).strip().lower()
    )

    if not starts_with_same_h1:
        header_el = etree.SubElement(body_el, f"{{{XHTML_NS}}}div")
        header_el.set("class", "doc-header")
        h1_el = etree.SubElement(header_el, f"{{{XHTML_NS}}}h1")
        h1_el.set("class", "doc-title")
        h1_el.set("id", "doc-top")
        h1_el.text = _ILLEGAL_XML_CHARS_RE.sub("", title)

        meta_p = etree.SubElement(header_el, f"{{{XHTML_NS}}}p")
        meta_p.set("class", "doc-meta")
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        meta_p.text = f"{author} • {date_str}"

    _append_bs4_children_to_xml(body_el, soup_body)

    xhtml_bytes = etree.tostring(
        html_el,
        xml_declaration=True,
        encoding="UTF-8",
        doctype="<!DOCTYPE html>",
        method="xml",
    )

    # Strict XML well-formedness check before packaging into EPUB
    etree.fromstring(xhtml_bytes)
    return xhtml_bytes


def convert_markdown_to_epub(
    title: str,
    markdown_text: str,
    settings: Settings,
    author: str | None = None,
    language: str | None = None,
) -> ConversionResult:
    """Convert Gemini Markdown + LaTeX into a valid e-ink EPUB 3 archive."""
    clean_title = _ILLEGAL_XML_CHARS_RE.sub("", title).strip() or "Gemini Document"
    clean_author = _ILLEGAL_XML_CHARS_RE.sub("", (author or settings.default_author)).strip() or "Gemini"
    clean_lang = (language or settings.default_language or "id").strip() or "id"

    # 1. Extract LaTeX safely before Markdown parsing
    protected_md, occurrences, unique_items = extract_latex_expressions(markdown_text)

    # 2. Batch-render all unique formulas to 300 DPI transparent PNG
    rendered_by_key, engine_used = render_math_batch(unique_items, settings=settings)

    href_by_key: dict[str, str] = {}
    images_to_embed: list[tuple[str, str, bytes]] = []
    unique_ok = 0
    unique_failed = 0

    for idx, item in enumerate(unique_items, start=1):
        res = rendered_by_key.get(item.key)
        if res and res.ok and res.png_bytes:
            file_name = f"images/math_{idx:04d}.png"
            uid = f"math_img_{idx:04d}"
            href_by_key[item.key] = file_name
            images_to_embed.append((uid, file_name, res.png_bytes))
            unique_ok += 1
        else:
            unique_failed += 1
            if res and res.error:
                logger.warning("Failed to render formula (%s): %s", item.latex[:60], res.error)

    # 3. Parse Markdown (GFM tables, strikethrough, fenced code, lists) with raw HTML escaped
    md_parser = MarkdownIt("commonmark", {"html": False, "typographer": False}).enable(
        ["table", "strikethrough"]
    )
    html_fragment = md_parser.render(protected_md)

    # 4. Inject math <img> tags, extract TOC headings, and build strict XHTML
    soup_body, toc_entries = _inject_math_and_toc(
        html_fragment=html_fragment,
        occurrences=occurrences,
        rendered_by_key=rendered_by_key,
        href_by_key=href_by_key,
    )
    xhtml_bytes = _build_strict_xhtml(
        title=clean_title,
        author=clean_author,
        language=clean_lang,
        soup_body=soup_body,
    )

    # 5. Package valid EPUB 3 using ebooklib
    book = epub.EpubBook()
    book.set_identifier(f"urn:uuid:{uuid.uuid4()}")
    book.set_title(clean_title)
    book.set_language(clean_lang)
    book.add_author(clean_author)
    book.add_metadata("DC", "date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    css_bytes = EINK_CSS_PATH.read_bytes()
    css_item = epub.EpubItem(
        uid="style_eink",
        file_name="style/eink.css",
        media_type="text/css",
        content=css_bytes,
    )
    book.add_item(css_item)

    for uid, file_name, png_bytes in images_to_embed:
        img_item = epub.EpubImage(
            uid=uid,
            file_name=file_name,
            media_type="image/png",
            content=png_bytes,
        )
        book.add_item(img_item)

    chapter = epub.EpubHtml(
        uid="chapter_main",
        title=clean_title,
        file_name="content.xhtml",
        lang=clean_lang,
    )
    chapter.set_content(xhtml_bytes)
    chapter.add_item(css_item)
    book.add_item(chapter)

    if toc_entries:
        book.toc = [
            epub.Link(f"content.xhtml#{anchor_id}", label, anchor_id)
            for _level, label, anchor_id in toc_entries
        ]
    else:
        book.toc = [epub.Link("content.xhtml", clean_title, "doc-top")]

    nav_item = epub.EpubNav()
    nav_item.add_item(css_item)
    book.add_item(nav_item)
    book.add_item(epub.EpubNcx())

    # Put main content first in spine so Kindle opens straight to the document
    book.spine = [chapter]

    with tempfile.TemporaryDirectory(prefix="g2k_epub_") as tmp_dir:
        out_path = Path(tmp_dir) / "output.epub"
        epub.write_epub(str(out_path), book, {"epub3_pages": False})
        epub_bytes = out_path.read_bytes()

    return ConversionResult(
        epub_bytes=epub_bytes,
        filename=sanitize_filename(clean_title),
        math_total=len(occurrences),
        math_unique_rendered=unique_ok,
        math_failed=unique_failed,
        math_engine=engine_used,
    )

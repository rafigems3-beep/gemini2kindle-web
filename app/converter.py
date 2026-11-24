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
_GDOCS_DISPLAY_RE = re.compile(r"(?<!\\)\\\$\\\$([\s\S]+?)\\\$\\\$")
_GDOCS_UNESCAPE_RE = re.compile(r"\\([\\$_+=<>\[\]()\-*.!#~`^])")
_GDOCS_MATH_SIGNAL_RE = re.compile(r"\\\\|\\[_=+<>\-]|[\^{}]")
_MATH_TOKEN_RE = re.compile(r"(G2KMATH(?:BLK|INL)\d+TOKEN)")

_BARE_ENV_NAMES = (
    r"equation\*?|align\*?|aligned|gather\*?|multline\*?|cases|"
    r"pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix|matrix|smallmatrix|array|split"
)
_BARE_ENV_RE = re.compile(rf"\\begin\{{({_BARE_ENV_NAMES})\}}[\s\S]+?\\end\{{\1\}}")

_TEX_COMMANDS: frozenset[str] = frozenset({
    # Fractions, roots, calculus, operators, chemistry
    "frac", "dfrac", "tfrac", "cfrac", "sqrt", "int", "iint", "iiint", "oint",
    "sum", "prod", "coprod", "lim", "limsup", "liminf", "sup", "inf",
    "max", "min", "arg", "det", "dim", "ker", "deg", "gcd", "hom",
    "exp", "log", "ln", "lg", "sin", "cos", "tan", "cot", "sec", "csc",
    "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh", "coth",
    "binom", "tbinom", "dbinom", "choose", "mod", "bmod", "pmod", "ce", "pu",
    # Greek letters
    "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta", "eta",
    "theta", "vartheta", "iota", "kappa", "lambda", "mu", "nu", "xi",
    "pi", "varpi", "rho", "varrho", "sigma", "varsigma", "tau", "upsilon",
    "phi", "varphi", "chi", "psi", "omega",
    "Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi", "Sigma", "Upsilon", "Phi", "Psi", "Omega",
    # Symbols, relations, arrows, logic
    "partial", "nabla", "infty", "hbar", "ell", "Re", "Im", "wp", "aleph",
    "emptyset", "varnothing", "angle", "measuredangle", "triangle", "perp", "parallel",
    "degree", "circ", "prime", "pm", "mp", "times", "div", "cdot", "cdots", "ldots",
    "vdots", "ddots", "dots", "ast", "star", "oplus", "otimes", "odot", "bullet",
    "cap", "cup", "sqcap", "sqcup", "setminus", "wedge", "vee", "neg", "lnot", "land", "lor",
    "leq", "le", "geq", "ge", "neq", "ne", "equiv", "approx", "cong", "sim", "simeq",
    "propto", "ll", "gg", "prec", "succ", "in", "notin", "ni",
    "subset", "subseteq", "supset", "supseteq",
    "to", "rightarrow", "leftarrow", "leftrightarrow", "Rightarrow", "Leftarrow", "Leftrightarrow",
    "implies", "iff", "mapsto", "longmapsto", "longrightarrow", "longleftarrow",
    "uparrow", "downarrow", "updownarrow", "rightleftharpoons", "xrightarrow", "xleftarrow",
    "forall", "exists", "nexists", "therefore", "because", "mid", "nmid", "models", "vdash", "vDash",
    # Delimiters, fonts, accents, decorations
    "left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr",
    "langle", "rangle", "lfloor", "rfloor", "lceil", "rceil", "lvert", "rvert", "lVert", "rVert",
    "text", "textrm", "textbf", "textit", "mathrm", "mathbf", "mathit", "mathbb",
    "mathcal", "mathfrak", "mathsf", "mathtt", "boldsymbol", "bm", "operatorname",
    "hat", "widehat", "bar", "overline", "underline", "vec", "tilde", "widetilde",
    "dot", "ddot", "dddot", "check", "breve", "acute", "grave",
    "overbrace", "underbrace", "overrightarrow", "overleftarrow",
    "cancel", "bcancel", "xcancel", "boxed", "color", "textcolor",
    "displaystyle", "textstyle", "scriptstyle", "limits", "nolimits",
    "stackrel", "overset", "underset", "quad", "qquad", "phantom", "not", "substack", "tag",
})

_TEX_BINARY_REL_CMDS: frozenset[str] = frozenset({
    "pm", "mp", "times", "div", "cdot", "ast", "star", "oplus", "otimes", "odot",
    "cap", "cup", "setminus", "wedge", "vee", "land", "lor",
    "leq", "le", "geq", "ge", "neq", "ne", "equiv", "approx", "cong", "sim", "simeq",
    "propto", "ll", "gg", "in", "notin", "subset", "subseteq", "supset", "supseteq",
    "to", "rightarrow", "leftarrow", "leftrightarrow", "Rightarrow", "Leftarrow", "Leftrightarrow",
    "implies", "iff", "mapsto", "longrightarrow", "longleftarrow", "rightleftharpoons",
    "xrightarrow", "xleftarrow", "mid", "nmid",
})

_MATH_IDENT_WORDS: frozenset[str] = frozenset({
    "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan",
    "sinh", "cosh", "tanh", "log", "ln", "exp", "lim", "max", "min", "det", "mod",
    "dx", "dy", "dz", "dt", "du", "dv", "dr", "ds", "dT", "dP", "dV", "dU", "dH", "dS", "dG", "dQ", "dW",
})

_TEX_CMD_NAME_RE = re.compile(r"(?<!\\)\\([A-Za-z]+)")
_BRACED_SCRIPT_RE = re.compile(r"[A-Za-z0-9)\]}][_^]\{[^{}\n]+\}")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_ANY_PLACEHOLDER_RE = re.compile(r"G2K(?:CODE|MATHBLK|MATHINL)\d+TOKEN")
_SNAKE_CASE_RE = re.compile(r"\b[a-z]{2,}_[a-z]{2,}\b")
_STANDALONE_BRACKET_BLOCK_RE = re.compile(r"(?m)^[ \t]*\[[ \t]*\n([\s\S]+?)\n[ \t]*\][ \t]*$")
_STANDALONE_BRACKET_SINGLE_RE = re.compile(r"(?m)^[ \t]*\[([^\n\[\]]+)\][ \t]*$")

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


_CODE_HINT_RE = re.compile(
    r"(?m)^\s*(?:def |class |import |from \w+ import |function |const |let |var |echo |return |#include|SELECT |CREATE TABLE|#!/)"
)


def _is_stray_prose_fence(info_str: str, inner_lines: list[str]) -> bool:
    """Detect accidental bare ``` fences wrapping prose + LaTeX (common in Gemini/Google Docs exports)."""
    if info_str.strip():
        return False
    inner = "\n".join(inner_lines).strip()
    if not inner or _CODE_HINT_RE.search(inner):
        return False
    has_explicit_math = bool(
        re.search(r"\\\([\s\S]+?\\\)|\\\[[\s\S]+?\\\]|\$\$[\s\S]+?\$\$|\\\$\\\$[\s\S]+?\\\$\\\$", inner)
    )
    if not has_explicit_math:
        return False
    # Prose wrapped by mistake typically has Markdown formatting or multi-word sentences
    word_count = len(re.findall(r"\b[A-Za-z]{3,}\b", inner))
    return word_count >= 6 or "**" in inner or "[cite:" in inner


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
            closed = False
            close_pattern = re.compile(rf"^[ ]{{0,3}}{re.escape(fence_char)}{{{fence_len},}}[ \t]*$")
            while i < n:
                block_lines.append(lines[i])
                if close_pattern.match(lines[i]):
                    closed = True
                    i += 1
                    break
                i += 1

            # Unwrap accidental bare ``` wrapping prose + LaTeX (e.g. Google Docs export artifact)
            if closed and fence_char == "`" and _is_stray_prose_fence(info_str, block_lines[1:-1]):
                out_lines.extend(block_lines[1:-1])
                continue

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


def _has_tex_command(text: str) -> bool:
    """Return True if text contains at least one recognized LaTeX math command."""
    return any(m.group(1) in _TEX_COMMANDS for m in _TEX_CMD_NAME_RE.finditer(text))


def _strip_tex_for_prose_check(text: str) -> str:
    """Remove TeX commands and brace arguments so only natural-language prose words remain."""
    cleaned = text
    # Strip \text{...}, \mathrm{...}, \ce{...}, etc. along with their brace content
    cleaned = re.sub(
        r"\\(?:text|textrm|textbf|textit|mathrm|mathbf|mathit|mathbb|mathcal|operatorname|ce|pu)\s*\{[^{}]*\}",
        " ",
        cleaned,
    )
    cleaned = _TEX_CMD_NAME_RE.sub(" ", cleaned)
    return cleaned


def _count_prose_words(text: str) -> int:
    """Count natural-language prose words (4+ letters, excluding math function names)."""
    stripped = _strip_tex_for_prose_check(text)
    words = re.findall(r"\b[A-Za-z]{4,}\b", stripped)
    return sum(1 for w in words if w.lower() not in _MATH_IDENT_WORDS)


def _looks_like_math_equation(text: str) -> bool:
    """Heuristically identify delimiter-less equations like ``E = mc^2`` or ``\\Delta U = Q - W``."""
    candidate = text.strip()
    if not candidate or len(candidate) > 600:
        return False
    if _URL_RE.search(candidate) or _ANY_PLACEHOLDER_RE.search(candidate):
        return False
    if _has_tex_command(candidate):
        return _count_prose_words(candidate) <= 2
    if _SNAKE_CASE_RE.search(candidate):
        return False
    if _count_prose_words(candidate) > 1:
        return False
    has_relation = any(sym in candidate for sym in ("=", "≈", "≤", "≥", "≠", "→", "⇒"))
    has_script = (
        "^" in candidate
        or bool(_BRACED_SCRIPT_RE.search(candidate))
        or bool(re.search(r"(?:^|[\s(+\-*/=])[A-Za-z]_[0-9A-Za-z](?![A-Za-z0-9_])", candidate))
    )
    return has_relation and has_script


def _extract_bare_brackets(text: str, register_math) -> str:
    """Extract display math written as standalone ``[ ... ]`` where ``\\`` was lost on copy."""

    def _replace_block(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner or _ANY_PLACEHOLDER_RE.search(inner) or _URL_RE.search(inner):
            return match.group(0)
        if _looks_like_math_equation(inner):
            return register_math(inner, display=True)
        return match.group(0)

    def _replace_single(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        # Never touch Markdown checkboxes [x], citations [1], or reference definitions
        if not inner or re.fullmatch(r"[xX\s]|\d+", inner):
            return match.group(0)
        if _ANY_PLACEHOLDER_RE.search(inner) or _URL_RE.search(inner):
            return match.group(0)
        if _looks_like_math_equation(inner):
            return register_math(inner, display=True)
        return match.group(0)

    text = _STANDALONE_BRACKET_BLOCK_RE.sub(_replace_block, text)
    text = _STANDALONE_BRACKET_SINGLE_RE.sub(_replace_single, text)
    return text


def _extract_bare_parens(text: str, register_math) -> str:
    """Extract inline math written as ``( ... )`` where ``\\`` was lost from ``\\(...\\)``."""
    out: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        if text[i] == "(":
            # Never touch Markdown links [label](url) or escaped \(
            prev_ch = text[i - 1] if i > 0 else ""
            if prev_ch in {"]", "\\"}:
                out.append("(")
                i += 1
                continue

            depth = 1
            j = i + 1
            aborted = False
            while j < n and depth > 0:
                if text[j] == "\n" and j + 1 < n and text[j + 1] == "\n":
                    aborted = True
                    break
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1

            if not aborted and depth == 0:
                inner = text[i + 1 : j - 1].strip()
                if (
                    inner
                    and not _ANY_PLACEHOLDER_RE.search(inner)
                    and not _URL_RE.search(inner)
                    and _has_tex_command(inner)
                    and _count_prose_words(inner) == 0
                ):
                    out.append(register_math(inner, display=False))
                    i = j
                    continue

        out.append(text[i])
        i += 1

    return "".join(out)


def _extract_bare_standalone_lines(text: str, register_math) -> str:
    """Detect standalone display equation lines that have no math delimiters at all."""
    md_block_prefix_re = re.compile(r"^[ \t]*(?:#{1,6}\s|>\s?|\||[-*+]\s|\d+[.)]\s)")
    out_lines: list[str] = []

    for line in text.split("\n"):
        stripped = line.strip()
        if (
            stripped
            and not md_block_prefix_re.match(line)
            and not _ANY_PLACEHOLDER_RE.search(stripped)
            and not _URL_RE.search(stripped)
            and _looks_like_math_equation(stripped)
        ):
            out_lines.append(register_math(stripped, display=True))
        else:
            out_lines.append(line)

    return "\n".join(out_lines)


def _read_balanced_group(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """Return index immediately after balanced group starting at ``start``, or ``start``."""
    n = len(text)
    if start >= n or text[start] != open_ch:
        return start
    depth = 1
    j = start + 1
    while j < n and depth > 0:
        if text[j] == "\\" and j + 1 < n:
            j += 2
            continue
        if text[j] == open_ch:
            depth += 1
        elif text[j] == close_ch:
            depth -= 1
        j += 1
    return j if depth == 0 else start


def _consume_tex_atom(text: str, start: int) -> int:
    """Consume a TeX command and all directly attached brace/script/\\left..\\right arguments."""
    n = len(text)
    cmd_match = re.match(r"\\([A-Za-z]+)", text[start:])
    if not cmd_match:
        return start
    cmd_name = cmd_match.group(1)
    if cmd_name not in _TEX_COMMANDS:
        return start

    j = start + cmd_match.end()

    # Special case: \left ... \right balanced group on the same line
    if cmd_name == "left":
        depth = 1
        scan = j
        while scan < n and depth > 0:
            if text.startswith(r"\left", scan) and (scan + 5 == n or not text[scan + 5].isalpha()):
                depth += 1
                scan += 5
            elif text.startswith(r"\right", scan) and (scan + 6 == n or not text[scan + 6].isalpha()):
                depth -= 1
                scan += 6
                if depth == 0:
                    # Include the delimiter right after \right (e.g. ), ], ., \|, \rangle)
                    if scan < n and text[scan] == "\\":
                        sub = re.match(r"\\(?:[A-Za-z]+|.)", text[scan:])
                        if sub:
                            scan += sub.end()
                    elif scan < n and text[scan] in ")]}|./>":
                        scan += 1
                    j = scan
                    break
            else:
                scan += 1

    # Consume attached optional [arg] (only for commands that take optional bracket args),
    # brace arguments {...}, and scripts ^{...}/_{...}/^x/_x
    opt_bracket_cmds = {"sqrt", "xrightarrow", "xleftarrow"}
    while j < n:
        if text[j] == "{":
            end_g = _read_balanced_group(text, j, "{", "}")
            if end_g > j:
                j = end_g
                continue
            break
        if text[j] == "[" and cmd_name in opt_bracket_cmds:
            end_b = _read_balanced_group(text, j, "[", "]")
            if end_b > j:
                j = end_b
                continue
            break
        if text[j] in "^_":
            if j + 1 < n and text[j + 1] == "{":
                end_s = _read_balanced_group(text, j + 1, "{", "}")
                if end_s > j + 1:
                    j = end_s
                    continue
            elif j + 1 < n and text[j + 1] == "\\":
                end_c = _consume_tex_atom(text, j + 1)
                if end_c > j + 1:
                    j = end_c
                    continue
            elif j + 1 < n and re.match(r"[A-Za-z0-9+\-']", text[j + 1]):
                j += 2
                continue
            break
        if text[j] == "'" and j + 1 <= n:
            j += 1
            continue
        break

    return j


def _tokenize_inline_segment(segment: str) -> list[tuple[str, int, int, str]]:
    """Tokenize a text segment into ``(kind, start, end, value)`` for bare inline LaTeX."""
    tokens: list[tuple[str, int, int, str]] = []
    i = 0
    n = len(segment)
    op_chars = set("=+-*/<>^_±×÷≈≤≥≠→⇒↔∞")

    while i < n:
        if segment[i].isspace():
            i += 1
            continue

        # TeX spacing command like \, \; \: \! (treated as non-anchor math glue)
        if segment[i] == "\\" and i + 1 < n and segment[i + 1] in ",;:! ":
            tokens.append(("GLUE", i, i + 2, segment[i : i + 2]))
            i += 2
            continue

        # Known TeX command anchor
        if segment[i] == "\\":
            end_tex = _consume_tex_atom(segment, i)
            if end_tex > i:
                val = segment[i:end_tex]
                cmd_m = re.match(r"\\([A-Za-z]+)", val)
                kind = "TEX_OP" if (cmd_m and cmd_m.group(1) in _TEX_BINARY_REL_CMDS) else "TEX"
                tokens.append((kind, i, end_tex, val))
                i = end_tex
                continue

        # Braced sub/superscript anchor e.g. x^{2}, CO_{2}, 10^{-5}
        braced_m = re.match(r"[A-Za-z0-9]+(?:[_^]\{[^{}\n]+\})+", segment[i:])
        if braced_m:
            end_b = i + braced_m.end()
            tokens.append(("TEX", i, end_b, segment[i:end_b]))
            i = end_b
            continue

        if segment[i] in "([":
            tokens.append(("POPEN", i, i + 1, segment[i]))
            i += 1
            continue
        if segment[i] in ")]":
            tokens.append(("PCLOSE", i, i + 1, segment[i]))
            i += 1
            continue
        if segment[i] == ",":
            tokens.append(("COMMA", i, i + 1, ","))
            i += 1
            continue
        if segment[i] in op_chars:
            j = i + 1
            while j < n and segment[j] in op_chars and segment[j] not in "^_":
                j += 1
            tokens.append(("OP", i, j, segment[i:j]))
            i = j
            continue

        # Number (including decimal like 3.14)
        num_m = re.match(r"\d+(?:\.\d+)?", segment[i:])
        if num_m:
            end_num = i + num_m.end()
            # Attach simple exponent/subscript like 10^8 or 2^n
            while end_num < n and segment[end_num] in "^_":
                if end_num + 1 < n and segment[end_num + 1] == "{":
                    e2 = _read_balanced_group(segment, end_num + 1, "{", "}")
                    if e2 > end_num + 1:
                        end_num = e2
                        continue
                elif end_num + 1 < n and re.match(r"[A-Za-z0-9+\-]", segment[end_num + 1]):
                    end_num += 2
                    continue
                break
            tokens.append(("NUM", i, end_num, segment[i:end_num]))
            i = end_num
            continue

        # Word / identifier
        word_m = re.match(r"[A-Za-z]+(?:[_^](?:\{[^{}\n]+\}|[A-Za-z0-9]))*'*", segment[i:])
        if word_m:
            end_w = i + word_m.end()
            val = segment[i:end_w]
            base_letters = re.match(r"[A-Za-z]+", val).group(0)
            has_math_script = "^" in val or ("_" in val and not _SNAKE_CASE_RE.search(val))
            if (
                len(base_letters) == 1
                or base_letters in _MATH_IDENT_WORDS
                or base_letters.lower() in _MATH_IDENT_WORDS
                or (has_math_script and len(base_letters) <= 2)
            ):
                tokens.append(("VAR", i, end_w, val))
            else:
                tokens.append(("PROSE", i, end_w, val))
            i = end_w
            continue

        # Anything else (punctuation ., ;, :, ?, !, markdown *, quotes, etc.) -> PROSE barrier
        tokens.append(("PROSE", i, i + 1, segment[i]))
        i += 1

    return tokens


def _balance_token_span(
    tokens: list[tuple[str, int, int, str]],
    left: int,
    right: int,
) -> tuple[int, int]:
    """Trim dangling punctuation, commas, operators, and unmatched parentheses/brackets."""
    while left <= right and tokens[left][0] in {"COMMA", "GLUE", "PCLOSE"}:
        left += 1
    while right >= left and tokens[right][0] in {"COMMA", "GLUE", "POPEN"}:
        right -= 1

    # Trim unmatched leading open delimiters and trailing close delimiters
    changed = True
    while changed and left <= right:
        changed = False
        pairs = {"(": ")", "[": "]"}
        stack: list[tuple[str, int]] = []
        unmatched_close: list[int] = []

        for idx in range(left, right + 1):
            kind, _, _, val = tokens[idx]
            if kind == "POPEN":
                stack.append((val, idx))
            elif kind == "PCLOSE":
                if stack and pairs.get(stack[-1][0]) == val:
                    stack.pop()
                else:
                    unmatched_close.append(idx)

        if unmatched_close:
            # Cut off at the first unmatched closing delimiter after all anchors
            first_bad_close = unmatched_close[0]
            if first_bad_close == right:
                right -= 1
                changed = True
            elif first_bad_close == left:
                left += 1
                changed = True
            else:
                # Prefer keeping the side containing an anchor
                has_anchor_left = any(tokens[k][0] in {"TEX", "TEX_OP"} for k in range(left, first_bad_close))
                if has_anchor_left:
                    right = first_bad_close - 1
                else:
                    left = first_bad_close + 1
                changed = True
            continue

        if stack:
            first_bad_open = stack[0][1]
            if first_bad_open == left:
                left += 1
                changed = True
            elif first_bad_open == right:
                right -= 1
                changed = True
            else:
                has_anchor_right = any(tokens[k][0] in {"TEX", "TEX_OP"} for k in range(first_bad_open + 1, right + 1))
                if has_anchor_right:
                    left = first_bad_open + 1
                else:
                    right = first_bad_open - 1
                changed = True
            continue

        while left <= right and tokens[left][0] in {"COMMA", "GLUE"}:
            left += 1
            changed = True
        while right >= left and tokens[right][0] in {"COMMA", "GLUE"}:
            right -= 1
            changed = True

    return left, right


def _extract_bare_inline_segment(segment: str, register_math) -> str:
    """Extract delimiter-less inline LaTeX spans from a segment free of hard boundaries."""
    if not _has_tex_command(segment) and not _BRACED_SCRIPT_RE.search(segment):
        return segment

    tokens = _tokenize_inline_segment(segment)
    anchor_indices = [idx for idx, tok in enumerate(tokens) if tok[0] in {"TEX", "TEX_OP"}]
    if not anchor_indices:
        return segment

    math_kinds = {"TEX", "TEX_OP", "OP", "NUM", "VAR", "POPEN", "PCLOSE", "COMMA", "GLUE"}
    op_kinds = {"OP", "TEX_OP"}

    raw_spans: list[tuple[int, int]] = []
    for anchor in anchor_indices:
        # Expand right across math tokens
        r = anchor
        while r + 1 < len(tokens) and tokens[r + 1][0] in math_kinds:
            # Don't consume a lone lowercase 'a'/'I' separated by space unless preceded by an operator
            nxt = tokens[r + 1]
            cur = tokens[r]
            if (
                nxt[0] == "VAR"
                and nxt[3] in {"a", "I"}
                and nxt[1] > cur[2]
                and cur[0] not in op_kinds
                and cur[0] != "POPEN"
            ):
                break
            r += 1

        # Expand left across math tokens with guard against English articles ('a', 'I')
        l = anchor
        while l - 1 >= 0 and tokens[l - 1][0] in math_kinds:
            prev = tokens[l - 1]
            cur = tokens[l]
            attached = prev[2] == cur[1]
            if prev[0] == "VAR" and not attached:
                # Only pull in a space-separated single-letter var if linked via operator or closing paren
                if cur[0] not in op_kinds and cur[0] not in {"POPEN", "PCLOSE"}:
                    # Allow cases like "f(x)" (handled by POPEN) or "2 \pi" (NUM) or "a \neq" (TEX_OP)
                    if not (len(prev[3]) == 1 and prev[3].isupper() and cur[0] == "TEX"):
                        break
            if prev[0] == "COMMA":
                break
            l -= 1

        if raw_spans and l <= raw_spans[-1][1] + 1:
            # Merge adjacent/overlapping math spans if separated only by math tokens (no PROSE)
            prev_l, prev_r = raw_spans[-1]
            if all(tokens[k][0] in math_kinds for k in range(prev_r + 1, l)):
                raw_spans[-1] = (prev_l, max(prev_r, r))
            else:
                raw_spans.append((l, r))
        else:
            raw_spans.append((l, r))

    # Build final segment replacements from right to left
    rebuilt = segment
    for l_idx, r_idx in reversed(raw_spans):
        bl, br = _balance_token_span(tokens, l_idx, r_idx)
        if bl > br:
            continue
        if not any(tokens[k][0] in {"TEX", "TEX_OP"} for k in range(bl, br + 1)):
            continue
        c_start = tokens[bl][1]
        c_end = tokens[br][2]
        expr = segment[c_start:c_end].strip()
        if not expr:
            continue
        tok_str = register_math(expr, display=False)
        rebuilt = rebuilt[:c_start] + tok_str + rebuilt[c_end:]

    return rebuilt


def _extract_bare_inline_math(text: str, register_math) -> str:
    """Extract inline bare LaTeX while respecting Markdown prefixes, tables, URLs, bold markers, and tokens."""
    md_prefix_re = re.compile(
        r"^([ \t]*(?:(?:>[ \t]*)*(?:#{1,6}[ \t]+|[-*+][ \t]+|\d+[.)][ \t]+)|(?:>[ \t]*)+))"
    )
    hard_split_re = re.compile(r"(\||G2K(?:CODE|MATHBLK|MATHINL)\d+TOKEN|https?://\S+|\*\*|__)")
    out_lines: list[str] = []

    for line in text.split("\n"):
        if not _has_tex_command(line) and not _BRACED_SCRIPT_RE.search(line):
            out_lines.append(line)
            continue

        prefix = ""
        rest = line
        prefix_m = md_prefix_re.match(line)
        if prefix_m:
            prefix = prefix_m.group(1)
            rest = line[len(prefix) :]

        parts = hard_split_re.split(rest)
        new_parts: list[str] = []
        for idx, part in enumerate(parts):
            # Odd indices are the captured hard delimiters -> keep verbatim
            if idx % 2 == 1 or not part:
                new_parts.append(part)
            else:
                new_parts.append(_extract_bare_inline_segment(part, register_math))
        out_lines.append(prefix + "".join(new_parts))

    return "\n".join(out_lines)


_UNICODE_DASHES_RE = re.compile(r"[–—−─━═]+")
_SEP_ROW_RE = re.compile(r"^[\s|:–—−─━═-]+$")
_INVISIBLE_ZW_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_NBSP_RE = re.compile(r"[\u00a0\u202f\u2007]")


def _split_table_cells(line: str) -> list[str]:
    """Split a Markdown pipe-table row into stripped cells (respecting escaped \\|)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith(r"\|"):
        s = s[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def _is_table_separator_row(line: str) -> bool:
    """Return True if line is a GFM table separator row (including Unicode dashes)."""
    s = line.strip()
    if not s or "|" not in s or not _SEP_ROW_RE.match(s):
        return False
    return bool(re.search(r"[-–—−─━═]", s))


def _is_strict_bordered_pipe_row(line: str) -> bool:
    s = line.strip()
    return len(s) >= 3 and s.startswith("|") and s.endswith("|") and not s.endswith(r"\|")


def _is_loose_pipe_row(line: str) -> bool:
    s = line.strip()
    if not s or re.match(r"^(?:#{1,6}\s|>\s?|[-*+]\s|\d+[.)]\s)", s):
        return False
    return bool(re.search(r"(?<!\\)\|", s))


def _format_separator_row(raw_sep_line: str | None, col_count: int) -> str:
    """Build a canonical ASCII GFM separator row with exact ``col_count`` columns."""
    raw_cells = _split_table_cells(raw_sep_line) if raw_sep_line else []
    out_cells: list[str] = []
    for idx in range(col_count):
        spec = raw_cells[idx] if idx < len(raw_cells) else "---"
        spec = _UNICODE_DASHES_RE.sub("---", spec).strip()
        left_colon = spec.startswith(":")
        right_colon = spec.endswith(":")
        if left_colon and right_colon:
            out_cells.append(":---:")
        elif left_colon:
            out_cells.append(":---")
        elif right_colon:
            out_cells.append("---:")
        else:
            out_cells.append("---")
    return "| " + " | ".join(out_cells) + " |"


def _format_data_row(cells: list[str], col_count: int) -> str:
    padded = list(cells[:col_count])
    while len(padded) < col_count:
        padded.append("")
    return "| " + " | ".join(padded) + " |"


def _normalize_markdown_tables(text: str) -> str:
    """Repair common Gemini copy quirks in GFM tables so markdown-it-py always renders <table>.

    Fixes:
    - Blank lines inserted between table rows
    - Unicode dashes (em/en/box-drawing) in separator rows
    - Excess leading indentation (4+ spaces) turning tables into code blocks
    - Mismatched column counts between header and separator rows
    - Bordered pipe tables missing a ``| --- |`` separator row
    - Missing blank lines before/after a table block
    """
    lines = text.split("\n")
    n = len(lines)
    out: list[str] = []
    i = 0

    while i < n:
        line_i = lines[i]
        if _is_loose_pipe_row(line_i) and not _is_table_separator_row(line_i):
            header_cells = _split_table_cells(line_i)
            col_count = len(header_cells)

            # Look ahead at next non-blank line (allowing at most 1 blank line after header)
            j = i + 1
            if j < n and not lines[j].strip():
                j += 1

            has_sep = j < n and _is_table_separator_row(lines[j]) and col_count >= 1
            has_implicit_bordered = (
                not has_sep
                and col_count >= 2
                and _is_strict_bordered_pipe_row(line_i)
                and j < n
                and _is_strict_bordered_pipe_row(lines[j])
                and len(_split_table_cells(lines[j])) == col_count
            )

            if has_sep or has_implicit_bordered:
                sep_line = lines[j] if has_sep else None
                cursor = j + 1 if has_sep else j

                body_rows: list[list[str]] = []
                while cursor < n:
                    cur_line = lines[cursor]
                    if not cur_line.strip():
                        # Collapse single blank line only if followed by another table row
                        nxt_idx = cursor + 1
                        if nxt_idx < n and _is_loose_pipe_row(lines[nxt_idx]) and not _is_table_separator_row(lines[nxt_idx]):
                            cursor = nxt_idx
                            continue
                        break
                    if _is_table_separator_row(cur_line):
                        cursor += 1
                        continue
                    if _is_loose_pipe_row(cur_line):
                        body_rows.append(_split_table_cells(cur_line))
                        cursor += 1
                        continue
                    break

                # Ensure blank line before table
                if out and out[-1].strip():
                    out.append("")

                out.append(_format_data_row(header_cells, col_count))
                out.append(_format_separator_row(sep_line, col_count))
                for row_cells in body_rows:
                    out.append(_format_data_row(row_cells, col_count))

                # Ensure blank line after table if more content follows
                if cursor < n and lines[cursor].strip():
                    out.append("")

                i = cursor
                continue

        out.append(line_i)
        i += 1

    return "\n".join(out)


def _unescape_gdocs_latex(raw: str) -> str:
    """Restore LaTeX commands escaped by Google Docs Markdown export (e.g. \\\\text -> \\text, \\_ -> _)."""
    return _GDOCS_UNESCAPE_RE.sub(r"\1", raw).strip()


def _extract_gdocs_escaped_math(text: str, register_math) -> str:
    """Extract Google-Docs-escaped LaTeX delimiters ``\\$\\$...\\$\\$`` and ``\\$...\\$``.

    Handles inner escaped currency like ``\\$\\\\\\$17{,}97\\\\text{M}\\$`` -> ``\\$17{,}97\\text{M}``
    while leaving standalone currency like ``(\\$23.2-35.2/t)`` or ``(2013\\$)`` untouched.
    """

    def _replace_gdocs_display(match: re.Match[str]) -> str:
        inner = _unescape_gdocs_latex(match.group(1))
        if not inner:
            return match.group(0)
        return register_math(inner, display=True)

    text = _GDOCS_DISPLAY_RE.sub(_replace_gdocs_display, text)

    out: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        if (
            text[i] == "\\"
            and i + 1 < n
            and text[i + 1] == "$"
            and _count_preceding_backslashes(text, i) == 0
        ):
            # Must not start with whitespace or another \$
            if i + 2 >= n or text[i + 2].isspace() or text[i + 2 : i + 4] == r"\$":
                out.append(r"\$")
                i += 2
                continue

            j = i + 2
            matched = False
            while j < n:
                ch = text[j]
                if ch == "|" or (ch == "\n" and j + 1 < n and text[j + 1] == "\n"):
                    break
                if ch == "\\" and j + 1 < n:
                    if text[j + 1] == "$" and _count_preceding_backslashes(text, j) == 0:
                        prev_ch = text[j - 1]
                        next_ch = text[j + 2] if j + 2 < n else ""
                        raw_inner = text[i + 2 : j]
                        if (
                            raw_inner
                            and not prev_ch.isspace()
                            and not next_ch.isdigit()
                            and _GDOCS_MATH_SIGNAL_RE.search(raw_inner)
                        ):
                            unescaped = _unescape_gdocs_latex(raw_inner)
                            if unescaped:
                                out.append(register_math(unescaped, display=False))
                                i = j + 2
                                matched = True
                                break
                        break
                    j += 2
                    continue
                j += 1

            if not matched:
                out.append(r"\$")
                i += 2
        else:
            out.append(text[i])
            i += 1

    return "".join(out)


_DISPLAY_BRACKET_MATH_SIGNAL_RE = re.compile(r"\\[A-Za-z]+|[=+^_<>{}≈≤≥≠→⇒×÷±∞∑∫]")


def _is_real_display_bracket_math(raw_inner: str) -> bool:
    """Distinguish true LaTeX ``\\[...\\]`` display math from Markdown-escaped bracket tags like ``\\[F\\]``."""
    stripped = raw_inner.strip()
    if not stripped:
        return False
    if "\n" in stripped:
        return True
    return bool(_DISPLAY_BRACKET_MATH_SIGNAL_RE.search(stripped))


def extract_latex_from_text_fragment(
    text: str,
    register_math,
    auto_detect_latex: bool = True,
) -> str:
    """Extract LaTeX from a plain text fragment (used for XHTML text nodes inside EPUBs)."""
    working = text.replace("\r\n", "\n").replace("\r", "\n")
    working = _ILLEGAL_XML_CHARS_RE.sub("", working)
    working = _NBSP_RE.sub(" ", working)
    working = _INVISIBLE_ZW_RE.sub("", working)

    def _replace_display_bracket(match: re.Match[str]) -> str:
        raw_inner = match.group(1)
        if not _is_real_display_bracket_math(raw_inner):
            return match.group(0)
        return register_math(raw_inner.strip(), display=True)

    def _replace_display_dollar(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner:
            return match.group(0)
        return register_math(inner, display=True)

    def _replace_inline_paren(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner:
            return match.group(0)
        return register_math(inner, display=False)

    working = _DISPLAY_BRACKET_RE.sub(_replace_display_bracket, working)
    working = _DISPLAY_DOLLAR_RE.sub(_replace_display_dollar, working)
    working = _INLINE_PAREN_RE.sub(_replace_inline_paren, working)
    working = _extract_inline_dollar_math(working, register_math)
    working = _extract_gdocs_escaped_math(working, register_math)

    if auto_detect_latex:
        working = _BARE_ENV_RE.sub(lambda m: register_math(m.group(0), display=True), working)
        working = _extract_bare_brackets(working, register_math)
        working = _extract_bare_parens(working, register_math)
        working = _extract_bare_standalone_lines(working, register_math)
        working = _extract_bare_inline_math(working, register_math)

    return working


def extract_latex_expressions(
    markdown_text: str,
    auto_detect_latex: bool = True,
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

    # Normalize invisible/non-breaking spaces outside code blocks (common in Gemini clipboard)
    working = _NBSP_RE.sub(" ", working)
    working = _INVISIBLE_ZW_RE.sub("", working)

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

    def _replace_display_bracket(match: re.Match[str]) -> str:
        raw_inner = match.group(1)
        if not _is_real_display_bracket_math(raw_inner):
            return match.group(0)
        return register_math(raw_inner.strip(), display=True)

    def _replace_display_dollar(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner:
            return match.group(0)
        return register_math(inner, display=True)

    def _replace_inline_paren(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner:
            return match.group(0)
        return register_math(inner, display=False)

    # 1) Explicit display math first so $$...$$ and \[...\] are never split by inline rules
    working = _DISPLAY_BRACKET_RE.sub(_replace_display_bracket, working)
    working = _DISPLAY_DOLLAR_RE.sub(_replace_display_dollar, working)

    # 2) Explicit inline math \(...\) and $...$
    working = _INLINE_PAREN_RE.sub(_replace_inline_paren, working)
    working = _extract_inline_dollar_math(working, register_math)

    # 3) Google Docs Markdown-exported escaped LaTeX (\$\$...\$\$ and \$...\$)
    working = _extract_gdocs_escaped_math(working, register_math)

    # 4) Auto-detect delimiter-less (bare) LaTeX often produced when copying from Gemini
    if auto_detect_latex:
        working = _BARE_ENV_RE.sub(lambda m: register_math(m.group(0), display=True), working)
        working = _extract_bare_brackets(working, register_math)
        working = _extract_bare_parens(working, register_math)
        working = _extract_bare_standalone_lines(working, register_math)
        working = _extract_bare_inline_math(working, register_math)

    # 5) Normalize Markdown tables (fixes Gemini NBSP, blank lines between rows, Unicode dashes, etc.)
    working = _normalize_markdown_tables(working)

    # 6) Restore protected code blocks & inline code (reverse order handles nested restores safely)
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
    auto_detect_latex: bool = True,
    render_latex: bool = True,
) -> ConversionResult:
    """Convert Gemini Markdown + LaTeX into a valid e-ink EPUB 3 archive."""
    clean_title = _ILLEGAL_XML_CHARS_RE.sub("", title).strip() or "Gemini Document"
    clean_author = _ILLEGAL_XML_CHARS_RE.sub("", (author or settings.default_author)).strip() or "Gemini"
    clean_lang = (language or settings.default_language or "id").strip() or "id"

    # 1. Extract LaTeX safely before Markdown parsing (optional if render_latex=False)
    if render_latex:
        protected_md, occurrences, unique_items = extract_latex_expressions(
            markdown_text,
            auto_detect_latex=auto_detect_latex,
        )
        # 2. Batch-render all unique formulas to 300 DPI transparent PNG
        rendered_by_key, engine_used = render_math_batch(unique_items, settings=settings)
    else:
        normalized = markdown_text.replace("\r\n", "\n").replace("\r", "\n")
        normalized = _ILLEGAL_XML_CHARS_RE.sub("", normalized)
        code_store: dict[str, str] = {}
        working = _protect_fenced_code(normalized, code_store)
        working = _protect_inline_code(working, code_store)
        working = _NBSP_RE.sub(" ", working)
        working = _INVISIBLE_ZW_RE.sub("", working)
        working = _normalize_markdown_tables(working)
        for token in reversed(list(code_store.keys())):
            working = working.replace(token, code_store[token])
        protected_md = working
        occurrences = {}
        unique_items = []
        rendered_by_key = {}
        engine_used = "disabled"

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

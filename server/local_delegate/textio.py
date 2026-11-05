"""Reading files into text, numbering lines, and estimating token counts."""

from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

TEXT_EXTS = {".txt", ".md", ".markdown", ".log", ".out", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
             ".toml", ".ini", ".cfg", ".xml", ".html", ".htm", ".css", ".py", ".r", ".rmd", ".qmd", ".sql",
             ".kt", ".kts", ".java", ".gradle", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".sh", ".ps1",
             ".bat", ".cmd", ".swift", ".go", ".rs", ".c", ".h", ".cpp", ".svg", ".tex", ".bib", ".properties",
             ".gitignore", ".env.example", ".rst", ".srt", ".vtt"}
CODE_EXTS = {".py", ".r", ".sql", ".kt", ".kts", ".java", ".gradle", ".ts", ".tsx", ".js", ".jsx", ".mjs",
             ".cjs", ".sh", ".ps1", ".bat", ".cmd", ".swift", ".go", ".rs", ".c", ".h", ".cpp", ".json",
             ".jsonl", ".yaml", ".yml", ".toml", ".xml", ".svg", ".html", ".css", ".log", ".out", ".csv", ".tsv"}

MAX_FILE_BYTES = 25_000_000


class ReadError(Exception):
    pass


def decode_bytes(data: bytes) -> str:
    """UTF-8 / UTF-8-BOM / UTF-16 (PowerShell 5 redirects write UTF-16LE) / cp1252 fallback."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if len(data) > 4 and data[1:4:2] == b"\x00\x00" and data[0:4:2] != b"\x00\x00":
        return data.decode("utf-16-le", errors="replace")  # BOM-less UTF-16LE
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _docx_text(path: Path) -> str:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ReadError(f"{path.name}: not a readable .docx ({exc})")
    root = ElementTree.fromstring(xml)
    lines: list[str] = []
    for para in root.iter(f"{ns}p"):
        parts: list[str] = []
        for node in para.iter():
            if node.tag == f"{ns}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{ns}tab":
                parts.append("\t")
            elif node.tag in (f"{ns}br", f"{ns}cr"):
                parts.append("\n")
        lines.append("".join(parts))
    return "\n".join(lines)


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader  # optional, only if the user installed it

        reader = PdfReader(str(path))
        pages = [(p.extract_text() or "") for p in reader.pages]
        text = "\n\n".join(f"[page {i + 1}]\n{t}" for i, t in enumerate(pages))
        if text.strip():
            return text
    except ImportError:
        pass
    except Exception as exc:
        raise ReadError(f"{path.name}: PDF text extraction failed ({exc})")
    exe = shutil.which("pdftotext")
    if exe:
        proc = subprocess.run([exe, "-layout", str(path), "-"], capture_output=True, timeout=120,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode == 0:
            return decode_bytes(proc.stdout)
    raise ReadError(f"{path.name}: reading PDFs locally needs 'pypdf' (py -3 -m pip install pypdf) "
                    "or pdftotext; scanned PDFs need OCR first")


def read_text(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ReadError(f"{path.name} is {size / 1e6:.0f} MB; split it or point at the relevant part")
    ext = path.suffix.lower()
    if ext == ".docx":
        return _docx_text(path)
    if ext == ".pdf":
        return _pdf_text(path)
    data = path.read_bytes()
    if ext not in TEXT_EXTS and b"\x00" in data[:4096] and not data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        raise ReadError(f"{path.name} looks binary; not sent to the model")
    return decode_bytes(data).replace("\r\n", "\n").replace("\r", "\n")


def number_lines(text: str, doc: str | None = None, start: int = 1) -> str:
    prefix = f"{doc}:" if doc else "L"
    return "\n".join(f"{prefix}{i}| {line}" for i, line in enumerate(text.split("\n"), start=start))


# ------------------------------------------------------------------ token estimation

HEBREW = re.compile(r"[֐-׿]")
FRENCH_HINTS = re.compile(r"\b(?:le|la|les|des|une|est|pour|avec|dans|vous|nous|qui|que|sur|pas)\b", re.I)


def content_kind(text: str, ext: str = "") -> str:
    """'he' | 'fr' | 'code' | 'prose' — picks the chars-per-token ratio."""
    sample = text[:20000]
    if not sample:
        return "prose"
    if len(HEBREW.findall(sample)) > 0.15 * max(1, len(re.findall(r"\w", sample))):
        return "he"
    if ext.lower() in CODE_EXTS:
        return "code"
    symbols = len(re.findall(r"[{}()\[\];=<>_/\\|#:]", sample))
    if symbols > 0.06 * len(sample):
        return "code"
    words = max(1, len(re.findall(r"[A-Za-zÀ-ÿ]+", sample)))
    if len(FRENCH_HINTS.findall(sample)) > 0.06 * words and re.search(r"[éèêàçùâîôû]", sample):
        return "fr"
    return "prose"


DEFAULT_RATIOS = {"prose": 3.8, "code": 3.2, "fr": 3.4, "he": 2.2}


def estimate_tokens(text: str, ratios: dict[str, float] | None = None, kind: str | None = None) -> int:
    r = dict(DEFAULT_RATIOS)
    if ratios:
        r.update({k: v for k, v in ratios.items() if v})
    kind = kind or content_kind(text)
    return int(len(text) / r.get(kind, 3.6)) + 1


def split_paragraph_chunks(text: str, max_chars: int) -> list[str]:
    """Split at blank lines (then lines) so each chunk stays under max_chars."""
    paras = re.split(r"(\n\s*\n)", text)
    chunks: list[str] = []
    cur = ""
    for piece in paras:
        if len(cur) + len(piece) <= max_chars:
            cur += piece
            continue
        if cur.strip():
            chunks.append(cur)
        cur = ""
        if len(piece) > max_chars:
            for line in piece.split("\n"):
                if len(cur) + len(line) + 1 > max_chars and cur:
                    chunks.append(cur)
                    cur = ""
                cur += line + "\n"
        else:
            cur = piece
    if cur.strip():
        chunks.append(cur)
    return chunks


def describe_size(chars: int, tokens: int) -> str:
    return f"{chars / 1000:.1f}k chars (~{tokens / 1000:.1f}k tokens)"


def read_many(paths: list[Path]) -> list[dict[str, Any]]:
    docs = []
    for i, p in enumerate(paths, start=1):
        text = read_text(p)
        docs.append({"id": f"D{i}", "path": str(p), "name": p.name, "text": text, "ext": p.suffix.lower()})
    return docs

# parsers.py
import re
from typing import List
from docx import Document

# <canvas_page> ... </canvas_page>  (case-insensitive, tolerant of whitespace)
_CANVAS_PAGE_RE = re.compile(
    r"<canvas_page\b[^>]*>(.*?)</canvas_page\s*>",
    re.IGNORECASE | re.DOTALL,
)

def extract_canvas_pages_from_text(text: str) -> List[str]:
    """Return a list of raw <canvas_page> blocks (inner content preserved)."""
    if not text:
        return []
    pages = []
    for m in _CANVAS_PAGE_RE.finditer(text):
        inner = m.group(1).strip()
        # keep page tags so downstream logic stays the same
        pages.append(f"<canvas_page>\n{inner}\n</canvas_page>")
    return pages

def extract_canvas_pages(docx_like) -> List[str]:
    """DOCX variant: read paragraphs, join with \\n, then use text splitter."""
    doc = Document(docx_like)
    text = "\n".join(p.text for p in doc.paragraphs)
    return extract_canvas_pages_from_text(text)

def scan_canvas_page_tags(text: str):
    """Simple counts to debug unbalanced tags in long docs."""
    starts = len(list(re.finditer(r"(?i)<canvas_page\b", text)))
    ends   = len(list(re.finditer(r"(?i)</canvas_page\s*>", text)))
    return {"starts": starts, "ends": ends, "balanced": (starts == ends)}

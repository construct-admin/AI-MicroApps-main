import re

TAG_RE_CACHE = {}

def _tag_re(tag: str):
    if tag not in TAG_RE_CACHE:
        TAG_RE_CACHE[tag] = re.compile(rf"<{tag}>\s*(.*?)\s*</{tag}>", re.IGNORECASE | re.DOTALL)
    return TAG_RE_CACHE[tag]

def extract_tag(tag: str, text: str, default: str = "") -> str:
    """
    Extracts <tag>value</tag> from `text` (case-insensitive). Returns `default` if not found.
    """
    if not text:
        return default
    m = _tag_re(tag).search(text)
    return (m.group(1).strip() if m else default)

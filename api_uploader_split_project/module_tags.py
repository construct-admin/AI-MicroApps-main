# module_tags.py
import re
from typing import List, Dict

MODULE_NAME_RE = re.compile(r"<\s*module_name\s*>\s*(.*?)\s*</\s*module_name\s*>",
                            re.IGNORECASE | re.DOTALL)
END_MODULE_RE   = re.compile(r"</\s*module\s*>", re.IGNORECASE)

def split_text_by_module_tags(text: str) -> List[Dict]:
    """
    Extracts sections like:
      <module_name>My Module</module_name>
      ...content...
      </module>

    Returns: [{"name": "My Module", "text": "...content..."}, ...]
    """
    out: List[Dict] = []
    pos = 0
    while True:
        m = MODULE_NAME_RE.search(text, pos)
        if not m:
            break
        name = (m.group(1) or "").strip()
        start_content = m.end()

        em = END_MODULE_RE.search(text, start_content)
        end_content = em.start() if em else len(text)

        content = text[start_content:end_content].strip()
        out.append({"name": name, "text": content})

        pos = em.end() if em else end_content
    return out

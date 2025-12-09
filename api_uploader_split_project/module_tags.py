# ------------------------------------------------------------------------------
# File: module_tags.py
# Refactor date: 2025-11-13
# Refactored by: Imaad Fakier
#
# Purpose:
#     Provide the parsing logic for extracting pseudo-XML “module sections”
#     from raw text inputs. This is used inside the Canvas Import micro-app to
#     split structured DOCX/PDF → text outputs into multiple Canvas module
#     items based on custom tags embedded in the authored content.
#
# Supported tag structure:
#
#       <module_name>My Module</module_name>
#           ... arbitrary content ...
#       </module>
#
# Output structure:
#       [
#           { "name": "My Module", "text": "... content ..." },
#           ...
#       ]
#
# Behaviour:
#     - Zero logic changes from original file.
#     - Tag matching is case-insensitive.
#     - Matching is greedy inside <module_name> ... </module>.
#     - Missing </module> tag gracefully falls back to end-of-text.
# ------------------------------------------------------------------------------

import re
from typing import List, Dict


# ==============================================================================
# Regular Expressions
# ==============================================================================

# Matches:
#   <module_name>Some Name</module_name>
# Captures the name (group 1).
MODULE_NAME_RE = re.compile(
    r"<\s*module_name\s*>\s*(.*?)\s*</\s*module_name\s*>", re.IGNORECASE | re.DOTALL
)

# Matches closing:
#   </module>
END_MODULE_RE = re.compile(r"</\s*module\s*>", re.IGNORECASE)


# ==============================================================================
# Public API
# ==============================================================================


def split_text_by_module_tags(text: str) -> List[Dict]:
    """
    Parse the input text and extract all module sections defined by:

        <module_name>Module Title</module_name>
            ...content...
        </module>

    Parameters:
        text (str):
            Raw text containing one or more pseudo-XML module blocks.

    Returns:
        List[Dict]:
            A list of objects of the form:
                {
                    "name": "<module title>",
                    "text": "<content between tags>"
                }

    Behaviour:
        - If a closing </module> is missing, extraction continues until EOF.
        - Strips whitespace around name and content.
        - Case-insensitive matching.
        - Non-overlapping sequential matching from left to right.
    """
    out: List[Dict] = []
    pos = 0

    while True:
        # Locate next module name tag
        m = MODULE_NAME_RE.search(text, pos)
        if not m:
            break

        name = (m.group(1) or "").strip()
        start_content = m.end()

        # Locate its corresponding </module> (optional)
        em = END_MODULE_RE.search(text, start_content)
        end_content = em.start() if em else len(text)

        content = text[start_content:end_content].strip()

        out.append({"name": name, "text": content})

        # Advance scanning position to after </module> or end of text
        pos = em.end() if em else end_content

    return out

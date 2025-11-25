# ------------------------------------------------------------------------------
# Refactor date: 2025-11-12
# Refactored by: Imaad Fakier
# Purpose: Align LaTeX Generator micro-app with OES GenAI Streamlit standards.
# ------------------------------------------------------------------------------
"""
LaTeX Generator (Refactored)
----------------------------
Streamlit entrypoint for OES' Image-to-LaTeX Generator micro-app.

Highlights in this refactor:
- Introduces consistent `.env` handling via `dotenv`.
- Adds SHA-256 access-code authentication aligned with other GenAI apps.
- Documents configuration schema, prompt structure, and system behavior.
- Preserves phase-driven input handling for multimodal GPT-4o vision model.
- Aligns with the unified OES Streamlit architecture (Alt-Text / Visual Transcripts / LO / Discussion patterns).

This file is intentionally declarative — it defines configuration, auth, and metadata
and defers heavy logic to the shared `core_logic.main` engine for consistency.
"""

import os
import hashlib
import streamlit as st
from dotenv import load_dotenv

# ------------------------------------------------------------------------------
# Environment setup
# ------------------------------------------------------------------------------
load_dotenv()  # Load .env file to access ACCESS_CODE_HASH and keys

# ------------------------------------------------------------------------------
# Streamlit page configuration
# ------------------------------------------------------------------------------
st.set_page_config(
    page_title="LaTeX Generator",
    page_icon="app_images/construct.webp",
    layout="centered",
    initial_sidebar_state="expanded",
)


# ------------------------------------------------------------------------------
# Authentication utilities
# ------------------------------------------------------------------------------
def _hash_code(input_code: str) -> str:
    """Hash an access code using SHA-256 for secure comparison."""
    return hashlib.sha256(input_code.encode("utf-8")).hexdigest()


ACCESS_CODE_HASH = os.getenv("ACCESS_CODE_HASH")
if not ACCESS_CODE_HASH:
    st.error(
        "⚠️ ACCESS_CODE_HASH not found in environment. "
        "Ask Engineering for the hashed access code and set it in the deployment environment."
    )
    st.stop()

# Initialize authentication state
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.title("🔒 Access Restricted")
    code_input = st.text_input(
        "Enter Access Code:", type="password", key="access_code_input"
    )

    if st.button("Submit", key="submit_access_code"):
        if _hash_code(code_input) == ACCESS_CODE_HASH:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect access code. Please try again.")

    st.stop()

# ------------------------------------------------------------------------------
# App metadata and configuration
# ------------------------------------------------------------------------------
APP_URL = "https://image-late-gen.streamlit.app/"
APP_IMAGE = "construct.webp"
PUBLISHED = True  # Status flag for deployment and UI toggle

APP_TITLE = "LaTeX Generator"
APP_INTRO = (
    "This app accepts uploaded images and returns properly formatted LaTeX code."
)
APP_HOW_IT_WORKS = """\
This app creates LaTeX code from images.
- For most images, it provides accurately formatted LaTeX (MathJax) code.
- Supports image-to-equation conversion for math-heavy visual content.
"""

# ------------------------------------------------------------------------------
# System prompt configuration
# ------------------------------------------------------------------------------
SYSTEM_PROMPT = """You accept images in url and file format containing mathematical equations, symbols, and text into accurate and you convert the images into properly formatted LaTeX code in MathJax. Output: Provide the final LaTeX (MathJax) code in a format that can be easily copied or exported.
### Output Requirements:
1. **Use `\dfrac{}` instead of `\frac{}`** when dealing with nested fractions for better readability.
2. **Ensure proper spacing** using `\,`, `\quad`, or `{}` where necessary.
3. **Avoid missing multipliers** like implicit multiplication (`\cdot`).
4. **Return only the LaTeX code** inside `$$` or `\[\]` for easy export.

### Example Output Format:
```latex
Re = 2 \dfrac{\dfrac{1}{2} \rho v_{\infty}^2 A}{\mu \dfrac{v_{\infty}}{l} A}"""

# ------------------------------------------------------------------------------
# Phase definition and configuration schema
# ------------------------------------------------------------------------------
PHASES = {
    "phase1": {
        "name": "Image Input and LaTeX Generation",
        "fields": {
            "uploaded_files": {
                "type": "file_uploader",
                "label": "Upload image(s) containing equations or symbols:",
                "allowed_files": ["png", "jpeg", "gif", "webp"],
                "multiple_files": True,
            },
        },
        "phase_instructions": "Upload images to generate LaTeX (MathJax) code.",
        "user_prompt": [
            {
                "condition": {},
                "prompt": (
                    "I am sending one or more images. Please generate separate LaTeX (MathJax) code blocks for each image.\n"
                    "The LaTeX code should match the equations exactly as they appear in the image."
                ),
            }
        ],
        "show_prompt": True,
        "allow_skip": False,
        "ai_response": True,
        "allow_revisions": True,
    }
}

# ------------------------------------------------------------------------------
# LLM configuration
# ------------------------------------------------------------------------------
PREFERRED_LLM = "gpt-4o"
LLM_CONFIG_OVERRIDE = {
    "gpt-4o": {
        "family": "openai",
        "model": "gpt-4o",
        "temperature": 0.4,
        "max_tokens": 800,
    }
}

# ------------------------------------------------------------------------------
# Debug and UI flags
# ------------------------------------------------------------------------------
SCORING_DEBUG_MODE = True
DISPLAY_COST = True
COMPLETION_MESSAGE = "Thanks for using the LaTeX Generator service."
COMPLETION_CELEBRATION = False
SIDEBAR_HIDDEN = True

# ------------------------------------------------------------------------------
# Sidebar controls
# ------------------------------------------------------------------------------
st.sidebar.button(
    "Logout", on_click=lambda: st.session_state.update({"authenticated": False})
)

# ------------------------------------------------------------------------------
# Entrypoint (defer to shared engine)
# ------------------------------------------------------------------------------
from core_logic.main import main

if __name__ == "__main__":
    main(config=globals())

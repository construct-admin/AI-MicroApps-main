# ------------------------------------------------------------------------------
# Refactor date: 2025-11-11
# Refactored by: Imaad Fakier
# Purpose: Align Discussion Generator micro-app with OES GenAI Streamlit standards.
# ------------------------------------------------------------------------------
"""
Alt Text Generator (Refactored)
--------------------------------
Streamlit entrypoint for OES' Alt Text Generator micro-app.

Highlights in this refactor:
- Stronger WCAG-aligned SYSTEM_PROMPT (factual, concise, non-redundant).
- Lowered default temperature for deterministic outputs (good for prod).
- Adds a small “file note” UI block (max files / size guidance).
- Keeps ACCESS_CODE_HASH auth as-is, but with clearer error text.
- Leaves the heavy lifting to `core_logic.main.main(config=globals())`.

This file is *intentionally* thin: it declares UI/config knobs and defers to
the shared engine so you can scale this pattern to other micro-apps.
"""

import os
import hashlib
import streamlit as st
from dotenv import load_dotenv

# Load environment variables from .env file at startup
load_dotenv()


# ------------------------------------------------------------------------------
# Page / App chrome
# ------------------------------------------------------------------------------
st.set_page_config(
    page_title="Alt Text Generator",
    page_icon="app_images/construct.webp",
    layout="centered",
    initial_sidebar_state="expanded",
)


# ------------------------------------------------------------------------------
# Small auth helper (unchanged behavior, clearer docstring)
# ------------------------------------------------------------------------------
def _hash_code(input_code: str) -> str:
    """
    Hash an access code using SHA-256. We compare hashes to avoid storing plaintext.
    """
    return hashlib.sha256(input_code.encode("utf-8")).hexdigest()


ACCESS_CODE_HASH = os.getenv("ACCESS_CODE_HASH")
if not ACCESS_CODE_HASH:
    st.error(
        "⚠️ ACCESS_CODE_HASH not found in environment. "
        "Ask Engineering for the hashed access code and set it in the deployment env."
    )
    st.stop()

# Session-level flag
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.title("🔒 Access Restricted")
    code = st.text_input("Enter Access Code:", type="password", key="access_code_input")
    if st.button("Submit", key="submit_access_code"):
        if _hash_code(code) == ACCESS_CODE_HASH:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect access code. Please try again.")
    st.stop()

# ------------------------------------------------------------------------------
# App metadata (used by core engine for UI/registry/observability)
# ------------------------------------------------------------------------------
APP_URL = "https://alt-text-bot.streamlit.app/"
APP_IMAGE = "construct.webp"
PUBLISHED = True  # Signals “production” behavior to the core engine (determinism etc.)

APP_TITLE = "Alt Text Generator"
APP_INTRO = "Upload image(s) and receive WCAG-aligned alt text for accessibility."

APP_HOW_IT_WORKS = """
This app creates alt text for accessibility from images.

- For most images, it provides **brief alt text** describing the most important information first.
- For **complex images** (charts, graphs, infographics), it provides:
  1) a **Short Description** (what it is), and
  2) a **Long Description** (what it conveys and how parts relate).

See <a href="https://www.w3.org/WAI/tutorials/images/" target="_blank">W3C Images Accessibility Guidelines</a>.
"""

# ------------------------------------------------------------------------------
# Prompt & Model config (tightened for production stability)
# ------------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You accept images (file or URL) and generate alt text that conforms to WCAG 2.2 AA.\n"
    "- Be factual and concise. Avoid aesthetic adjectives (e.g., 'beautiful').\n"
    "- Avoid redundancy (do not say 'image of').\n"
    "- If the user indicates text is important, transcribe text verbatim.\n"
    "- For complex images, output BOTH a Short and a Long description; "
    "  the Long description explains relationships and the key insight."
)

PHASES = {
    "phase1": {
        "name": "Image Input and Alt Text Generation",
        "fields": {
            "uploaded_files": {
                "type": "file_uploader",
                "label": "Choose files",
                "allowed_files": ["png", "jpeg", "gif", "webp", "jpg"],
                "multiple_files": True,
                "key": "file_uploader",
                "help": "Accepted: PNG, JPEG, GIF, WEBP, JPG",
            },
            # purely informational UI label to surface operational guardrails
            "file_note": {
                "type": "markdown",
                "body": "📎 *Upload up to 6 images, max 8 MB each.*",
                "key": "file_note",
                "decorative": True,  # core engine won’t treat this as an input
            },
            "important_text": {
                "type": "checkbox",
                "label": "The text in my image(s) is important",
                "value": False,
                "help": (
                    "If text in the image is important, it should be included verbatim. "
                    "If it is irrelevant or present elsewhere on the page, omit it."
                ),
                "key": "important_text_checkbox",
            },
            "complex_image": {
                "type": "checkbox",
                "label": "My image is a complex image (chart, infographic, etc.)",
                "value": False,
                "help": "Complex images get a Short and a Long description.",
                "key": "complex_image_checkbox",
            },
        },
        "phase_instructions": "Generate WCAG-compliant alt text for the provided images.",
        "user_prompt": [
            {
                "condition": {"important_text": False, "complex_image": False},
                "prompt": (
                    "I am sending you one or more images. Provide separate appropriate alt text for each image. "
                    "Each alt text must describe the most important concept in ≤120 characters."
                ),
            },
            {
                "condition": {"complex_image": True, "important_text": False},
                "prompt": (
                    "I am sending complex image(s). Provide:\n\n"
                    "**Short Description:**\n[Short Description]\n\n"
                    "**Long Description:**\n[Long Description]\n"
                    "(Explain relationships between parts and the key insight conveyed.)"
                ),
            },
            {
                "condition": {"important_text": True, "complex_image": False},
                "prompt": (
                    "I am sending you one or more images. Provide separate appropriate alt text for each image. "
                    "Each alt text must describe the most important concept in ≤120 characters and transcribe any text verbatim."
                ),
            },
            {
                "condition": {"important_text": True, "complex_image": True},
                "prompt": (
                    "I am sending complex image(s). Provide:\n\n"
                    "**Short Description:**\n[Short Description]\n\n"
                    "**Long Description:**\n[Long Description]\n"
                    "(Explain relationships between parts and transcribe any text verbatim.)"
                ),
            },
            # fallback / context stitch
            {
                "condition": {},
                "prompt": "Here are the uploaded images - {http_img_urls} and uploaded files.",
            },
        ],
        "show_prompt": True,
        "allow_skip": False,
    }
}

PREFERRED_LLM = "gpt-4o"  # Multimodal vision model
LLM_CONFIG_OVERRIDE = {
    # Lower temperature for consistent, production-grade outputs.
    "temperature": 0.4,
    "max_tokens": 900,
}

# Show cost/debug in test; hide in prod if needed.
SCORING_DEBUG_MODE = True
DISPLAY_COST = True

COMPLETION_MESSAGE = "Thanks for using the Alt Text Generator service"
COMPLETION_CELEBRATION = False  # confetti is cute; off by default for production feel

SIDEBAR_HIDDEN = True

# Sidebar logout
st.sidebar.button(
    "Logout",
    on_click=lambda: st.session_state.update({"authenticated": False}),
    key="logout_button",
)

# ------------------------------------------------------------------------------
# Entrypoint (defer to shared engine)
# ------------------------------------------------------------------------------
from core_logic.main import main

if __name__ == "__main__":
    main(config=globals())

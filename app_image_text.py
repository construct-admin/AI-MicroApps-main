# ------------------------------------------------------------------------------
# Refactor date: 2025-11-12
# Refactored by: Imaad Fakier
# Purpose: Align Text from Image Generator micro-app with OES GenAI Streamlit standards.
# ------------------------------------------------------------------------------
"""
Text from Image Generator (Refactored)
--------------------------------------
Streamlit entrypoint for OES' Text-from-Image Generator micro-app.

Highlights in this refactor:
- Introduces `.env` loading with `dotenv` for unified environment management.
- Implements SHA-256 access-code authentication aligned with other GenAI apps.
- Adds structured documentation for metadata, prompts, and configuration schema.
- Preserves simple multimodal GPT-4o image-to-text extraction pattern.
- Fully aligned with OES GenAI Streamlit standards (Alt-Text, LaTeX, LO, Discussion patterns).

This file defines configuration, auth, and UI metadata, while delegating execution
and model orchestration to `core_logic.main` for consistency and maintainability.
"""

import os
import hashlib
import streamlit as st
from dotenv import load_dotenv

# ------------------------------------------------------------------------------
# Environment setup
# ------------------------------------------------------------------------------
load_dotenv()  # Load environment variables from .env file

# ------------------------------------------------------------------------------
# Streamlit page configuration
# ------------------------------------------------------------------------------
st.set_page_config(
    page_title="Text from Image Generator",
    page_icon="app_images/construct.webp",
    layout="centered",
    initial_sidebar_state="expanded",
)


# ------------------------------------------------------------------------------
# Authentication utilities
# ------------------------------------------------------------------------------
def _hash_code(input_code: str) -> str:
    """Hash the provided access code using SHA-256 for secure comparison."""
    return hashlib.sha256(input_code.encode("utf-8")).hexdigest()


ACCESS_CODE_HASH = os.getenv("ACCESS_CODE_HASH")
if not ACCESS_CODE_HASH:
    st.error(
        "⚠️ ACCESS_CODE_HASH not found in environment. "
        "Ask Engineering for the hashed access code and configure it in deployment settings."
    )
    st.stop()

# Initialize authentication state
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.title("🔒 Access Restricted")
    input_code = st.text_input(
        "Enter Access Code:", type="password", key="access_code_input"
    )

    if st.button("Submit", key="submit_access_code"):
        if _hash_code(input_code) == ACCESS_CODE_HASH:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect access code. Please try again.")

    st.stop()  # Prevent unauthorized access beyond this point

# ------------------------------------------------------------------------------
# App metadata and configuration
# ------------------------------------------------------------------------------
APP_URL = "https://image-text-gen.streamlit.app/"
APP_IMAGE = "construct.webp"
PUBLISHED = True  # Indicates production-ready configuration

APP_TITLE = "Text from Image Generator"
APP_INTRO = (
    "This micro-app accepts uploaded images and returns the text featured within them."
)
APP_HOW_IT_WORKS = """\
This app extracts text directly from images using GPT-4o's vision capabilities.
- Upload one or more images containing text.
- The model will return the transcribed text exactly as it appears (verbatim).
"""

# ------------------------------------------------------------------------------
# System prompt configuration
# ------------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You accept images (file uploads or URLs) and extract text from them exactly as it appears (verbatim). "
    "Maintain the original casing, spacing, and punctuation from the source image."
)

# ------------------------------------------------------------------------------
# Phase definition and configuration schema
# ------------------------------------------------------------------------------
PHASES = {
    "phase1": {
        "name": "Image Input and Text Extraction",
        "fields": {
            "uploaded_files": {
                "type": "file_uploader",
                "label": "Upload one or more images containing text:",
                "allowed_files": ["png", "jpeg", "gif", "webp"],
                "multiple_files": True,
            },
        },
        "phase_instructions": "Upload image(s) and extract all visible text verbatim.",
        "user_prompt": [
            {
                "condition": {},
                "prompt": (
                    "I am sending one or more images. Please return the extracted text from each image "
                    "exactly as it appears — preserving capitalization, punctuation, and line breaks."
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
        "temperature": 0.3,
        "max_tokens": 700,
    }
}

# ------------------------------------------------------------------------------
# Debug and UI flags
# ------------------------------------------------------------------------------
SCORING_DEBUG_MODE = True
DISPLAY_COST = True
COMPLETION_MESSAGE = "Thanks for using the Text from Image Generator service."
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

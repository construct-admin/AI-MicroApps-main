# ------------------------------------------------------------------------------
# Refactor date: 2025-11-12
# Refactored by: Imaad Fakier
# Purpose: Align Motion Graphic Script Generator micro-app with OES GenAI Streamlit standards.
# ------------------------------------------------------------------------------
"""
Motion Graphic Script Generator (Refactored)
--------------------------------------------
Streamlit entrypoint for OES' Motion Graphic Script Generator micro-app.

Highlights in this refactor:
- Introduces `.env` loading via `dotenv` for consistent environment handling.
- Adds SHA-256 access-code authentication consistent with other GenAI apps.
- Documents helper functions and the integrated RAG (retrieval-augmented generation) workflow.
- Preserves PDF-based content ingestion using `fitz` (PyMuPDF) for contextual grounding.
- Provides structured system prompt emphasizing table-format scripting with visual cues.
- Aligns with the unified OES GenAI Streamlit app pattern (Alt-Text / LO / Discussion / LaTeX / Image Text).

This file defines metadata, configuration, and prompt logic while delegating execution
to the shared `core_logic.main` engine for standardization and observability.
"""

import os
import hashlib
import fitz  # PyMuPDF for PDF processing
import streamlit as st
from dotenv import load_dotenv

# ------------------------------------------------------------------------------
# Environment setup
# ------------------------------------------------------------------------------
load_dotenv()  # Load variables from .env for consistent key handling

# ------------------------------------------------------------------------------
# Streamlit page configuration
# ------------------------------------------------------------------------------
st.set_page_config(
    page_title="Motion Graphic Script Generator",
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
        "Ask Engineering for the hashed access code and configure it in the deployment environment."
    )
    st.stop()

# Initialize session state for authentication
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
APP_URL = "https://motion-graphic-script-gen.streamlit.app/"
APP_IMAGE = "construct.webp"
PUBLISHED = True

APP_TITLE = "Motion Graphic Script Generator"
APP_INTRO = "Use this application to generate motion graphic scripts for academic video content."

# ------------------------------------------------------------------------------
# RAG (Retrieval-Augmented Generation) configuration
# ------------------------------------------------------------------------------
RAG_IMPLEMENTATION = True  # Enables PDF ingestion to enhance context
SOURCE_DOCUMENT = "rag_docs/ABETSIS_C1_M0_V1.pdf"  # Reference PDF document path

# ------------------------------------------------------------------------------
# System prompt configuration
# ------------------------------------------------------------------------------
SYSTEM_PROMPT = """Develop and refine academic video scripts (750 - 800 words) for motion graphic videos that blend information and visuals to improve comprehension and retention.

Visuals: Incorporate advanced visual elements like 2.5D animations, high-quality renderings, and immersive simulations to transform static information into dynamic content. 

Output format:
Format scripts as a table, with columns for approximate time, text, and visual cues. Because we should have a change in visuals every 15 seconds, I create the script in 15 - 20-second increments, with each increment being a row in the table. You work on the assumption that each minute of video will have 120 words of text.

Scripts will include the following sections:
 1) Begin with an engaging hook.
 2) Add a paragraph that establishes the relevance of the learning material to real-world practices.
 3) Present the key theory or ideas of the content.
 4) Provide an explanation of how that links to solving the problems presented at the start.
 5) A conclusion that pulls the video together, tying key points explored in the script.
"""


# ------------------------------------------------------------------------------
# PDF text extraction helper
# ------------------------------------------------------------------------------
def extract_text_from_pdf(pdf_path: str) -> str:
    """Extracts plain text from a PDF using PyMuPDF (fitz).

    Args:
        pdf_path (str): Path to the source PDF.

    Returns:
        str: Combined text from all pages for contextual grounding.
    """
    text = ""
    with fitz.open(pdf_path) as pdf:
        for page in pdf:
            text += page.get_text("text")
    return text


# ------------------------------------------------------------------------------
# Dynamic user prompt builder
# ------------------------------------------------------------------------------
def build_user_prompt(user_input: dict) -> str:
    """Constructs a dynamic prompt combining user input and optional RAG-sourced text.

    The builder integrates learning objectives, content, and academic stage,
    optionally appending extracted text from a reference PDF for enhanced context.

    Args:
        user_input (dict): Collected UI input fields from Streamlit.

    Returns:
        str: Fully composed prompt string for model input.

    Raises:
        ValueError: If required input fields are missing or RAG fails.
    """
    try:
        learning_objectives = user_input.get("learning_objectives", "").strip()
        learning_content = user_input.get("learning_content", "").strip()
        academic_stage = user_input.get("academic_stage_radio", "").strip()

        # Validate required fields
        if not learning_objectives:
            raise ValueError("The 'Learning Objectives' field is required.")
        if not learning_content:
            raise ValueError("The 'Learning Content' field is required.")
        if not academic_stage:
            raise ValueError("An 'Academic Stage' must be selected.")

        # Integrate RAG context (truncated to 2,000 chars to stay within token limits)
        document_text = ""
        if RAG_IMPLEMENTATION and os.path.exists(SOURCE_DOCUMENT):
            document_text = extract_text_from_pdf(SOURCE_DOCUMENT)[:2000]

        # Construct user prompt
        return (
            f"{SYSTEM_PROMPT}\n\n"
            f"Example Template / Training Context:\n{document_text}\n\n"
            f"The motion graphic script should align with the following objectives: {learning_objectives}.\n"
            f"Base the motion graphic content on this learning material: {learning_content}.\n"
            f"Please align tone, depth, and complexity to the {academic_stage} academic level."
        )

    except Exception as e:
        raise ValueError(f"Error building prompt: {str(e)}")


# ------------------------------------------------------------------------------
# UI schema and phase definition
# ------------------------------------------------------------------------------
PHASES = {
    "generate_motion_graphic_script": {
        "name": "Motion Graphic Script Generator",
        "fields": {
            "learning_objectives": {
                "type": "text_area",
                "label": "Enter module-level learning objective(s):",
                "height": 500,
            },
            "learning_content": {
                "type": "text_area",
                "label": "Enter learning content (used as the script base):",
                "height": 500,
            },
            "academic_stage_radio": {
                "type": "radio",
                "label": "Select the academic stage of learners:",
                "options": [
                    "Lower Primary",
                    "Middle Primary",
                    "Upper Primary",
                    "Lower Secondary",
                    "Upper Secondary",
                    "Undergraduate",
                    "Postgraduate",
                ],
            },
        },
        "phase_instructions": (
            "Provide learning objectives, relevant content, and the academic stage to generate a script."
        ),
        "user_prompt": [
            {
                "condition": {},
                "prompt": "The motion graphic should align with: {learning_objectives}.",
            },
            {
                "condition": {},
                "prompt": "Base the script on the following content: {learning_content}.",
            },
            {
                "condition": {},
                "prompt": "Ensure alignment with academic level: {academic_stage_radio}.",
            },
        ],
        "ai_response": True,
        "allow_revisions": True,
        "show_prompt": True,
        "read_only_prompt": False,
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
        "temperature": 0.5,
        "top_p": 0.9,
        "frequency_penalty": 0.5,
        "presence_penalty": 0.3,
    }
}

# ------------------------------------------------------------------------------
# UI and operational flags
# ------------------------------------------------------------------------------
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

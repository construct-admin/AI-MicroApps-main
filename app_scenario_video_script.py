# ------------------------------------------------------------------------------
# Refactor date: 2025-11-12
# Refactored by: Imaad Fakier
# Purpose: Align Scenario Video Script Generator micro-app with OES GenAI Streamlit standards.
# ------------------------------------------------------------------------------
"""
Scenario Video Script Generator (Refactored)
-------------------------------------------
Streamlit entrypoint for OES' Scenario Video Script Generator micro-app.

Highlights in this refactor:
- Adds `.env` loading via `dotenv` for environment handling consistency.
- Implements unified SHA-256 access-code authentication for app-level access control.
- Documents helper functions and improves RAG (Retrieval-Augmented Generation) structure.
- Provides structured logging, prompt documentation, and standardized sectioning.
- Maintains the table-format instructional video script style aligned to academic practice.

This file defines metadata, configuration, and prompt logic while delegating execution
and orchestration to the shared `core_logic.main` engine for standardization.
"""

import os
import hashlib
import fitz  # PyMuPDF for PDF text extraction
import streamlit as st
from dotenv import load_dotenv

# ------------------------------------------------------------------------------
# Environment setup
# ------------------------------------------------------------------------------
load_dotenv()

# ------------------------------------------------------------------------------
# Streamlit page configuration
# ------------------------------------------------------------------------------
st.set_page_config(
    page_title="Scenario Video Script Generator",
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
APP_URL = "https://scenario-video-script-gen.streamlit.app/"
APP_IMAGE = "construct.webp"
PUBLISHED = True

APP_TITLE = "Scenario Video Script Generator"
APP_INTRO = "Use this application to generate scenario-based academic video scripts."

# ------------------------------------------------------------------------------
# RAG (Retrieval-Augmented Generation) configuration
# ------------------------------------------------------------------------------
RAG_IMPLEMENTATION = True  # Enable RAG integration
SOURCE_DOCUMENT = "rag_docs/ABETSIS_C1_M1_V1.pdf"  # Path to source PDF
RAG_TRUNCATION_CHARS = 2000  # Safety token limit

# ------------------------------------------------------------------------------
# System prompt configuration
# ------------------------------------------------------------------------------
SYSTEM_PROMPT = """Develop and refine academic video scripts for students that will support them in developing an understanding of how key academic concepts are applied in real-world practices. The academic script will be used for a scenario video.

Visuals: Incorporate visual elements that tell a story.

Output format:
Format scripts as a table, with columns for approximate time, text, and visual cues. Because we should have a change in visuals every 15 seconds, I create the script in 15 - 20-second increments, with each increment being a row in the table. You work on the assumption that each minute of video will have 120 words of text.

Scripts will include the following sections:
 1) Begin with an engaging hook. This may be a reference to a person, story, interesting statistic or case study, or a critical question from the content that should be engaged with.  Avoid pretext. Usually, we don’t need to tell them what we’re going to talk about.  We can just start talking about it. The pretext is different from a hook.  
 2) Following the introductory paragraph, add a paragraph that establishes the relevance of the learning material to real-world practices.
 3) Present the key theory or ideas of the content.
 4) Provide an explanation of how that links to solving the problems presented at the start.
 5) A conclusion that pulls the video together. The conclusion should tie together the main points explored in the script. It can be presented as key takeaways, or as though-provoking questions for reflection.
"""


# ------------------------------------------------------------------------------
# PDF text extraction helper
# ------------------------------------------------------------------------------
def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract plain text from a PDF using PyMuPDF (fitz)."""
    text = ""
    try:
        with fitz.open(pdf_path) as pdf:
            for page in pdf:
                text += page.get_text("text")
    except Exception as e:
        st.warning(f"Could not extract text from {pdf_path}: {e}")
    return text


# ------------------------------------------------------------------------------
# Dynamic user prompt builder
# ------------------------------------------------------------------------------
def build_user_prompt(user_input: dict) -> str:
    """Construct a dynamic prompt combining user inputs and RAG-sourced context."""
    try:
        learning_objectives = user_input.get("learning_objectives", "").strip()
        learning_content = user_input.get("learning_content", "").strip()
        academic_stage = user_input.get("academic_stage_radio", "").strip()

        # Validate required inputs
        if not learning_objectives:
            raise ValueError("The 'Learning Objectives' field is required.")
        if not learning_content:
            raise ValueError("The 'Learning Content' field is required.")
        if not academic_stage:
            raise ValueError("An 'Academic Stage' must be selected.")

        # Load RAG context if applicable
        document_text = ""
        if RAG_IMPLEMENTATION and os.path.exists(SOURCE_DOCUMENT):
            document_text = extract_text_from_pdf(SOURCE_DOCUMENT)[
                :RAG_TRUNCATION_CHARS
            ]

        # Build user prompt
        return (
            f"{SYSTEM_PROMPT}\n\n"
            f"Example Template / Context Reference:\n{document_text}\n\n"
            f"The scenario video script should align with these objectives: {learning_objectives}.\n"
            f"Base the scenario on this content: {learning_content}.\n"
            f"Ensure tone and complexity align to {academic_stage} academic level."
        )

    except Exception as e:
        raise ValueError(f"Error building prompt: {str(e)}")


# ------------------------------------------------------------------------------
# UI schema and phase definition
# ------------------------------------------------------------------------------
PHASES = {
    "generate_scenario_video_script": {
        "name": "Scenario Video Script Generator",
        "fields": {
            "learning_objectives": {
                "type": "text_area",
                "label": "Enter module-level learning objective(s):",
                "height": 500,
            },
            "learning_content": {
                "type": "text_area",
                "label": "Enter relevant content to form the script foundation:",
                "height": 500,
            },
            "academic_stage_radio": {
                "type": "radio",
                "label": "Select academic stage of learners:",
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
            "Provide learning objectives, content, and academic stage to generate a structured scenario video script."
        ),
        "user_prompt": [
            {
                "condition": {},
                "prompt": "The script should align with the objectives: {learning_objectives}.",
            },
            {
                "condition": {},
                "prompt": "Base the script on the content: {learning_content}.",
            },
            {
                "condition": {},
                "prompt": "Ensure tone matches the {academic_stage_radio} level.",
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

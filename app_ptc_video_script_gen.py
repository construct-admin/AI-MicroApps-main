# ------------------------------------------------------------------------------
# Refactor date: 2025-11-25
# Refactored by: Imaad Fakier
# Purpose: Align PTC Video Script Generator micro-app with OES GenAI Streamlit standards.
# ------------------------------------------------------------------------------
"""
PTC Video Script Generator (Refactored)
---------------------------------------
Streamlit entrypoint for OES' Presenter-to-Camera (PTC) Video Script Generator micro-app.

Highlights in this refactor:
- Adds `.env` loading via `dotenv` for consistent environment handling.
- Implements SHA-256 access-code authentication aligned with other GenAI apps.
- Documents helper functions and integrates a simple RAG pathway using PyMuPDF (`fitz`).
- Cleans up debug prints, fixes undefined variable references, and clarifies token truncation.
- Preserves the table-format script guidance and production metadata.

This file defines metadata, configuration, and prompt logic while delegating execution
and model orchestration to the shared `core_logic.main` engine for consistency and observability.
"""

import os
import hashlib
import logging
import fitz  # PyMuPDF for PDF processing
import streamlit as st
from dotenv import load_dotenv

# ------------------------------------------------------------------------------
# Environment setup
# ------------------------------------------------------------------------------
load_dotenv()  # Load env vars from .env (e.g., ACCESS_CODE_HASH)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ptc_script_gen")

# ------------------------------------------------------------------------------
# Streamlit page configuration
# ------------------------------------------------------------------------------
st.set_page_config(
    page_title="PTC Video Script Generator",
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
APP_URL = "https://ptc-video-script-gen.streamlit.app/"
APP_IMAGE = "construct.webp"
PUBLISHED = True

APP_TITLE = "PTC Video Script Generator"
APP_INTRO = (
    "Use this application to generate Presenter-to-Camera (PTC) academic video scripts."
)

# ------------------------------------------------------------------------------
# RAG (Retrieval-Augmented Generation) configuration
# ------------------------------------------------------------------------------
RAG_IMPLEMENTATION = True  # Enables PDF ingestion to enhance context
SOURCE_DOCUMENT = "rag_docs/PTC_Example_Pages_2_3_4.pdf"  # Reference PDF document path
RAG_TRUNCATION_CHARS = 2000  # Safety limit for token budget

# ------------------------------------------------------------------------------
# System prompt configuration
# ------------------------------------------------------------------------------
SYSTEM_PROMPT = """
Develop and support the development of high-quality academic video scripts of 780 - 880 words.

All scripts should contain the following: 
1. Hook: Start each video with something that grabs the viewer’s attention immediately. This could be a surprising fact, an intriguing question, an intriguing example, or a metaphor/analogy. The aim is to spark curiosity and encourage viewers to stay engaged. 
2. Learning Objectives: Following the hook, include 1-2 learning objectives in the video introduction. The video objectives should be aligned with Bloom´s taxonomy. 
3. Real-World Examples: Following the introductory paragraph, include a paragraph that explains the relevance of the learning material. Draw connections between the video content and real-life scenarios, or share a personal story to bring the material to life. 
4. Develop 3 to 4 body paragraphs 
5. Conclusion: Tie together the main points of the video and leave learners with a clear understanding of what they have learned and how they can apply it. 

Writing style and tone: 
- Writing should be clear and succinct 
- Adopt a conversational tone

The example script in the pdf should be used to inform the tone, style, and structure of responses. Format scripts as a table with columns for text, and visual cues.

---
STRICT OUTPUT REQUIREMENTS — DO NOT IGNORE

You MUST output the final script ONLY as a MARKDOWN TABLE with TWO columns:

| Text | Visual Cues |

Rules:
- The full narrative of the video script appears in the **Text** column.
- The **Visual Cues** column provides visual guidance corresponding to each section or paragraph.
- Each section of the script must begin with a timing indicator, in parentheses, at the start of the Text cell. Example:
(0:00–0:25) <script text>

The output MUST include the following sections in order:
1. Hook
2. Learning Objectives
3. Real-World Examples
4. Body Paragraph 1
5. Body Paragraph 2
6. Body Paragraph 3
7. Conclusion

Do NOT include any explanation, commentary, notes, or formatting outside of the table.
Do NOT output the script as plain paragraphs.
Do NOT introduce camera directions or scene instructions.
BEGIN OUTPUT WITH THE TABLE HEADER ROW.
"""

# ------------------------------------------------------------------------------
# PDF text extraction helper
# ------------------------------------------------------------------------------


def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract plain text from a PDF using PyMuPDF (fitz).

    Args:
        pdf_path: Path to the source PDF.
    Returns:
        Combined text from all pages for contextual grounding.
    """
    text = ""
    try:
        with fitz.open(pdf_path) as pdf:
            for page in pdf:
                text += page.get_text("text")
    except Exception as e:
        logger.warning("Failed to read PDF '%s': %s", pdf_path, e)
    return text


# ------------------------------------------------------------------------------
# Dynamic user prompt builder
# ------------------------------------------------------------------------------


def build_user_prompt(user_input: dict) -> str:
    """Compose a dynamic prompt combining user input and (optional) RAG text.\n\n    Validates required fields, ingests a reference PDF when available, and returns a\n    single prompt string for the model. PDF text is truncated to stay within token limits.\n\n    Args:
        user_input: UI values collected by the shared engine.
    Returns:
        A composed prompt string suitable for model input.
    Raises:
        ValueError: When required inputs are missing.
    """
    # Extract and validate inputs
    learning_objectives = (user_input.get("learning_objectives", "") or "").strip()
    learning_content = (user_input.get("learning_content", "") or "").strip()
    academic_stage = (user_input.get("academic_stage_radio", "") or "").strip()

    if not learning_objectives:
        raise ValueError("The 'Learning Objectives' field is required.")
    if not learning_content:
        raise ValueError("The 'Learning Content' field is required.")
    if not academic_stage:
        raise ValueError("An 'Academic Stage' must be selected.")

    # RAG context (optional)
    document_text = ""
    if RAG_IMPLEMENTATION and os.path.exists(SOURCE_DOCUMENT):
        document_text = extract_text_from_pdf(SOURCE_DOCUMENT)
        if document_text:
            document_text = document_text[:RAG_TRUNCATION_CHARS]

    # Compose final prompt
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Example Template / Training Context:\n{document_text}\n\n"
        f"The PTC video script should align with the following objectives: {learning_objectives}.\n"
        f"Base the script on this learning content: {learning_content}.\n"
        f"Please align tone, depth, and complexity to the {academic_stage} academic level."
    )
    return prompt


# ------------------------------------------------------------------------------
# UI schema and phase definition
# ------------------------------------------------------------------------------
PHASES = {
    "generate_ptc_script": {
        "name": "PTC Video Script Generator",
        "fields": {
            "learning_objectives": {
                "type": "text_area",
                "label": "Enter the relevant module-level learning objective(s):",
                "height": 300,
            },
            "learning_content": {
                "type": "text_area",
                "label": "Enter relevant learning content that will serve as the basis for the PTC video script.",
                "height": 500,
            },
            "academic_stage_radio": {
                "type": "radio",
                "label": "Select the academic stage of the students:",
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
            "Provide the relevant details (learning objectives, content, and academic stage) to generate a PTC script."
        ),
        "user_prompt": [
            {
                "condition": {},
                "prompt": "The PTC video script should be aligned with the provided objectives: {learning_objectives}.",
            },
            {
                "condition": {},
                "prompt": "Base the PTC video script on the following content: {learning_content}.",
            },
            {
                "condition": {},
                "prompt": "Please align the PTC video script to the following academic stage level: {academic_stage_radio}.",
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

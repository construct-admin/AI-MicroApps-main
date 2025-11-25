# ------------------------------------------------------------------------------
# Refactor date: 2025-11-12
# Refactored by: Imaad Fakier
# Purpose: Align Discussion Generator micro-app with OES GenAI Streamlit standards.
# ------------------------------------------------------------------------------
"""
Construct Learning Objectives (LO) Generator (Refactored)
----------------------------------------------------------
Streamlit entrypoint for OES' Construct Learning Objectives micro-app.

Highlights in this refactor:
- Introduces consistent `.env` handling via `dotenv`.
- Adds SHA-256 access-code authentication aligned with other GenAI apps.
- Documents helper functions and configuration logic for maintainability.
- Preserves phase-based configuration and dynamic prompt building.
- Aligns with the unified OES Streamlit architecture (Alt-Text / Visual Transcripts pattern).

This file is intentionally declarative — it defines configuration, auth, and UI metadata
and defers heavy logic to the shared `core_logic.main` engine for consistent behavior.
"""

import os
import hashlib
import streamlit as st
from dotenv import load_dotenv

# ------------------------------------------------------------------------------
# Environment setup
# ------------------------------------------------------------------------------
load_dotenv()  # Load variables from .env

# ------------------------------------------------------------------------------
# Streamlit page configuration
# ------------------------------------------------------------------------------
st.set_page_config(
    page_title="Construct LO Generator",
    page_icon="app_images/construct.webp",
    layout="centered",
    initial_sidebar_state="expanded",
)


# ------------------------------------------------------------------------------
# Authentication utilities
# ------------------------------------------------------------------------------
def hash_code(input_code: str) -> str:
    """Hash an access code using SHA-256 for secure comparison."""
    return hashlib.sha256(input_code.encode()).hexdigest()


ACCESS_CODE_HASH = os.getenv("ACCESS_CODE_HASH")
if not ACCESS_CODE_HASH:
    st.error("⚠️ ACCESS_CODE_HASH not found. Please configure it in your environment.")
    st.stop()

# Initialize authentication state
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

# Access control UI
if not st.session_state.authenticated:
    st.title("🔒 Access Restricted")
    access_code_input = st.text_input("Enter Access Code:", type="password")

    if st.button("Submit"):
        if hash_code(access_code_input) == ACCESS_CODE_HASH:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect access code. Please try again.")

    st.stop()  # Prevent unauthorized access beyond this point

# ------------------------------------------------------------------------------
# App metadata and configuration
# ------------------------------------------------------------------------------
PUBLISHED = True
APP_URL = "https://ai-microapps-ltajmcd53aypk3cp7mxsey.streamlit.app/"
APP_IMAGE = "construct.webp"

APP_TITLE = "Construct Learning Objectives Generator"
APP_INTRO = (
    "This micro-app allows you to generate learning objectives or validate alignment for existing objectives."
    " It streamlines instructional design by integrating AI to enhance efficiency and personalization."
)
APP_HOW_IT_WORKS = """\
1. Fill in the details of your course/module.
2. Configure cognitive goals and relevance preferences.
3. Generate specific, measurable, and aligned learning objectives.
"""

SYSTEM_PROMPT = (
    "You are EduDesignGPT, an expert instructional designer specialized in creating clear, specific, and measurable "
    "module-level learning objectives for online courses."
)


# ------------------------------------------------------------------------------
# Dynamic condition builders
# ------------------------------------------------------------------------------
def get_objective_prompts():
    """Return a list of dynamic prompt conditions for objective generation."""
    return [
        {
            "condition": {"title_lo": True},
            "prompt": "Please suggest {lo_quantity} module-learning objectives for the provided module title: {title}.",
        },
        {
            "condition": {"c_lo": True},
            "prompt": "Please write {lo_quantity} module-learning objectives based on the provided course learning objectives: {course_lo}.",
        },
        {
            "condition": {"q_lo": True},
            "prompt": "Please write {lo_quantity} module-learning objectives based on the provided graded assessment questions: {quiz_lo}.",
        },
        {
            "condition": {"f_lo": True},
            "prompt": "Please write {lo_quantity} module-learning objectives based on the provided formative activity questions: {form_lo}.",
        },
        {
            "condition": {"m_lo": True},
            "prompt": "Please write {lo_quantity} module-learning objectives based on the provided module content: {mc_lo}.",
        },
    ]


def get_bloom_taxonomy_conditions():
    """Return Bloom's taxonomy focus prompts based on cognitive goal checkboxes."""
    return [
        {
            "condition": {},
            "prompt": (
                "Please generate the learning objectives in the following strict Bloom’s "
                "Taxonomy order (lowest → highest cognitive demand):\n"
                "1. Remember\n"
                "2. Understand\n"
                "3. Apply\n"
                "4. Analyze\n"
                "5. Evaluate\n"
                "6. Create\n\n"
                "Only include the levels selected by the user, but always output them in this "
                "fixed sequence to ensure proper scaffolding and reduced cognitive load."
            ),
        },
        {"condition": {"goal_rem": True}, "prompt": "Remember."},
        {"condition": {"goal_under": True}, "prompt": "Understand."},
        {"condition": {"goal_apply": True}, "prompt": "Apply."},
        {"condition": {"goal_analyze": True}, "prompt": "Analyze."},
        {"condition": {"goal_evaluate": True}, "prompt": "Evaluate."},
        {"condition": {"goal_create": True}, "prompt": "Create."},
    ]


def get_relevance_conditions():
    """Return optional focus prompts to drive real-world relevance and alignment."""
    return [
        {
            "condition": {"real_world_relevance": True},
            "prompt": "Provide objectives relevant to real-world practices and industry trends.",
        },
        {
            "condition": {"problem_solving": True},
            "prompt": "Provide objectives that emphasize problem-solving and critical thinking.",
        },
        {
            "condition": {"meta_cognitive_reflection": True},
            "prompt": "Provide objectives focusing on meta-cognitive reflection.",
        },
        {
            "condition": {"ethical_consideration": True},
            "prompt": "Provide objectives that integrate emotional, moral, and ethical considerations.",
        },
    ]


def get_academic_stage_conditions():
    """Return alignment prompts for academic level specificity."""
    return [
        {
            "condition": {},
            "prompt": "Please align the learning objectives to the following academic stage level: \n Level:",
        },
        {
            "condition": {"academic_stage_radio": "Lower Primary"},
            "prompt": "Lower Primary.",
        },
        {
            "condition": {"academic_stage_radio": "Middle Primary"},
            "prompt": "Middle Primary.",
        },
        {
            "condition": {"academic_stage_radio": "Upper Primary"},
            "prompt": "Upper Primary.",
        },
        {
            "condition": {"academic_stage_radio": "Lower Secondary"},
            "prompt": "Lower Secondary.",
        },
        {
            "condition": {"academic_stage_radio": "Upper Secondary"},
            "prompt": "Upper Secondary.",
        },
        {
            "condition": {"academic_stage_radio": "Undergraduate"},
            "prompt": "Undergraduate.",
        },
        {
            "condition": {"academic_stage_radio": "Postgraduate"},
            "prompt": "Postgraduate.",
        },
    ]


# ------------------------------------------------------------------------------
# Phase definition and configuration schema
# ------------------------------------------------------------------------------
PHASES = {
    "generate_objectives": {
        "name": "Generate Learning Objectives",
        "fields": {
            # Request type selection
            "learning_obj_choices": {
                "type": "markdown",
                "body": "<h3>What would you like to do?</h3>",
                "unsafe_allow_html": True,
            },
            # Checkbox input groups
            "title_lo": {
                "type": "checkbox",
                "label": "Suggest objectives based on the module title",
            },
            "m_lo": {
                "type": "checkbox",
                "label": "Generate objectives based on module description",
            },
            "c_lo": {
                "type": "checkbox",
                "label": "Based on course learning objectives",
            },
            "q_lo": {
                "type": "checkbox",
                "label": "Based on graded assessment questions",
            },
            "f_lo": {
                "type": "checkbox",
                "label": "Based on formative activity questions",
            },
            # Text fields and sliders
            "title": {
                "type": "text_input",
                "label": "Module title",
                "showIf": {"title_lo": True},
            },
            "course_lo": {
                "type": "text_area",
                "label": "Course learning objectives",
                "height": 300,
                "showIf": {"c_lo": True},
            },
            "quiz_lo": {
                "type": "text_area",
                "label": "Graded assessment question(s)",
                "height": 300,
                "showIf": {"q_lo": True},
            },
            "form_lo": {
                "type": "text_area",
                "label": "Formative activity question(s)",
                "height": 300,
                "showIf": {"f_lo": True},
            },
            "mc_lo": {
                "type": "text_area",
                "label": "Module description",
                "height": 200,
                "showIf": {"m_lo": True},
            },
            "lo_quantity": {
                "type": "slider",
                "label": "Number of objectives",
                "min_value": 1,
                "max_value": 6,
                "value": 3,
            },
            # Preferences and relevance
            "relevance_preferences": {
                "type": "markdown",
                "body": "<h3>Preferences:</h3> Select focus areas.",
                "unsafe_allow_html": True,
            },
            "real_world_relevance": {
                "type": "checkbox",
                "label": "Focus on real-world relevance.",
            },
            "problem_solving": {
                "type": "checkbox",
                "label": "Focus on problem-solving.",
            },
            "meta_cognitive_reflection": {
                "type": "checkbox",
                "label": "Include meta-cognitive reflection.",
            },
            "ethical_consideration": {
                "type": "checkbox",
                "label": "Include emotional, moral, and ethical aspects.",
            },
            # Bloom's taxonomy and academic stage
            "bloom_taxonomy": {
                "type": "markdown",
                "body": "<h3>Bloom's Taxonomy</h3> Select goals:",
                "unsafe_allow_html": True,
            },
            "goal_rem": {"type": "checkbox", "label": "Remember"},
            "goal_under": {"type": "checkbox", "label": "Understand"},
            "goal_apply": {"type": "checkbox", "label": "Apply"},
            "goal_analyze": {"type": "checkbox", "label": "Analyze"},
            "goal_evaluate": {"type": "checkbox", "label": "Evaluate"},
            "goal_create": {"type": "checkbox", "label": "Create"},
            "academic_stage": {
                "type": "markdown",
                "body": "<h3>Academic Stage</h3>",
                "unsafe_allow_html": True,
            },
            "academic_stage_radio": {
                "type": "radio",
                "label": "Select the academic stage of learners.",
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
        "phase_instructions": "Dynamically build a prompt based on all selected parameters.",
        "user_prompt": (
            get_objective_prompts()
            + get_relevance_conditions()
            + get_bloom_taxonomy_conditions()
            + get_academic_stage_conditions()
        ),
        "ai_response": True,
        "allow_revisions": True,
        "show_prompt": True,
        "read_only_prompt": False,
    }
}

# ------------------------------------------------------------------------------
# LLM and runtime configuration
# ------------------------------------------------------------------------------
PREFERRED_LLM = "gpt-4o"
LLM_CONFIG_OVERRIDE = {
    "gpt-4o": {"family": "openai", "model": "gpt-4o", "temperature": 0.3}
}
SIDEBAR_HIDDEN = True


# ------------------------------------------------------------------------------
# Prompt builder
# ------------------------------------------------------------------------------
def build_user_prompt(user_input: dict) -> str:
    """Build a composite user prompt dynamically based on UI selections."""
    try:
        # --- First collect all prompt parts ---
        raw_parts = [
            config["prompt"].format(
                **{key: user_input.get(key, "") for key in config["condition"].keys()}
            )
            for config in PHASES["generate_objectives"]["user_prompt"]
            if all(
                user_input.get(key) == value
                for key, value in config["condition"].items()
            )
        ]

        # --- Bloom ordering logic (smart method) ---
        BLOOM_ORDER = {
            "Remember.": 1,
            "Understand.": 2,
            "Apply.": 3,
            "Analyze.": 4,
            "Evaluate.": 5,
            "Create.": 6,
        }

        bloom_parts = []
        non_bloom_parts = []

        for part in raw_parts:
            # Exact match to Bloom-level prompts
            if part.strip() in BLOOM_ORDER:
                bloom_parts.append(part)
            else:
                non_bloom_parts.append(part)

        # Sort Bloom parts by their order index
        bloom_parts_sorted = sorted(
            bloom_parts, key=lambda x: BLOOM_ORDER.get(x.strip(), 999)
        )

        # Recombine (keep all non-Bloom prompts as-is)
        user_prompt_parts = non_bloom_parts + bloom_parts_sorted

        return "\n".join(user_prompt_parts)

    except KeyError as e:
        raise ValueError(f"Missing key in user input: {e}")


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

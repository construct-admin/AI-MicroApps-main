# ------------------------------------------------------------------------------
# Refactor date: 2025-11-11
# Refactored by: Imaad Fakier
# Purpose: Align Discussion Generator micro-app with OES GenAI Streamlit standards.
# ------------------------------------------------------------------------------
"""
core_logic.main (Refactored)
----------------------------
Shared Streamlit engine that renders phases, builds inputs, formats prompts,
handles LLM execution via family handlers, logs results, and manages UX state.

Key improvements in this refactor:
- Upload guardrails (MAX_FILES / MAX_BYTES) to prevent OOM and latency spikes.
- Deterministic behavior in production (cap temperature when PUBLISHED=True).
- More robust prompt formatting (safer .format usage).
- Lightweight chat history trimming to avoid session bloat.
- Small UX polish (clearer errors; consistent variable names).

NOTE:
- This preserves existing public surface area expected by app entrypoints.
- It assumes `core_logic.llm_config.LLM_CONFIG` and `core_logic.handlers.HANDLERS`
  are present (unchanged interface), and `StorageManager` is available.
"""

from __future__ import annotations

import re
import os
import base64
import mimetypes
from datetime import datetime
from typing import Any, Dict, List

import pandas as pd
import streamlit as st
from streamlit import _bottom
from streamlit_extras.stylable_container import stylable_container
from streamlit_extras.let_it_rain import rain

from core_logic.handlers import HANDLERS
from core_logic.llm_config import LLM_CONFIG
from core_logic.data_storage import StorageManager

# ---------------------------------------------------------------------------
# Operational guardrails (tune per app)
# ---------------------------------------------------------------------------
MAX_FILES = 6  # Operationally safe upper bound for Community cloud
MAX_BYTES = 8 * 1024 * 1024  # 8 MB per file


# ---------------------------------------------------------------------------
# Conditional logic evaluation
# ---------------------------------------------------------------------------
def evaluate_conditions(user_input: Dict[str, Any], condition: Dict[str, Any]) -> bool:
    """
    Evaluate a Mongo-like condition document against user_input.
    Supports $and, $or, $not and standard comparison operators ($gt, $lt, $eq, ...).
    """
    if "$and" in condition:
        return all(evaluate_conditions(user_input, c) for c in condition["$and"])
    if "$or" in condition:
        return any(evaluate_conditions(user_input, c) for c in condition["$or"])
    if "$not" in condition:
        return not evaluate_conditions(user_input, condition["$not"])

    for key, value in condition.items():
        if isinstance(value, dict):
            operator, cond_val = next(iter(value.items()))
            user_val = user_input.get(key)
            if operator == "$gt" and not (user_val > cond_val):
                return False
            if operator == "$lt" and not (user_val < cond_val):
                return False
            if operator == "$gte" and not (user_val >= cond_val):
                return False
            if operator == "$lte" and not (user_val <= cond_val):
                return False
            if operator == "$eq" and not (user_val == cond_val):
                return False
            if operator == "$ne" and not (user_val != cond_val):
                return False
            if operator == "$in" and user_val not in cond_val:
                return False
            if operator == "$nin" and user_val in cond_val:
                return False
        else:
            # simple equality or membership
            if isinstance(value, list):
                if user_input.get(key) not in value:
                    return False
            else:
                if user_input.get(key) != value:
                    return False
    return True


# ---------------------------------------------------------------------------
# Dynamic input field builder
# ---------------------------------------------------------------------------
def build_field(
    phase_name: str,
    fields: Dict[str, Dict[str, Any]],
    user_input: Dict[str, Any],
    phases: Dict[str, Any],
    system_prompt: str,
) -> None:
    """
    Render input fields for a phase based on declarative `fields` config.
    Honors optional 'showIf' conditions and gracefully disables fields that were
    already answered in a previous pass.
    """
    function_map = {
        "text_input": st.text_input,
        "text_area": st.text_area,
        "warning": st.warning,
        "button": st.button,
        "radio": st.radio,
        "markdown": st.markdown,
        "selectbox": st.selectbox,
        "checkbox": st.checkbox,
        "slider": st.slider,
        "number_input": st.number_input,
        "image": st.image,
        "file_uploader": st.file_uploader,
        "chat_input": st.chat_input,
    }

    for field_key, field in fields.items():
        # 1) Conditional visibility
        if "showIf" in field and not evaluate_conditions(user_input, field["showIf"]):
            continue

        # 2) Extract config → kwargs for Streamlit widget
        kwargs: Dict[str, Any] = {}
        for k in (
            "label",
            "body",
            "value",
            "index",
            "max_chars",
            "help",
            "on_click",
            "options",
            "horizontal",
            "min_value",
            "max_value",
            "step",
            "height",
            "unsafe_allow_html",
            "placeholder",
            "image",
            "width",
            "caption",
            "label_visibility",
            "initial_assistant_message",
        ):
            if k in field and field[k] not in (None, ""):
                kwargs[k if k != "unsafe_allow_html" else "unsafe_allow_html"] = field[
                    k
                ]

        # Map uploader config keys to streamlit args
        if field.get("allowed_files"):
            kwargs["type"] = field["allowed_files"]
        if field.get("multiple_files"):
            kwargs["accept_multiple_files"] = True

        field_type = field.get("type", "")
        my_input_function = function_map.get(field_type)
        if not my_input_function:
            st.warning(f"Unknown field type: {field_type}")
            continue

        # 3) Re-display previously entered answer (disabled)
        status_key = f"{phase_name}_phase_status"
        if status_key in st.session_state and st.session_state[status_key]:
            ss_key = f"{phase_name}_user_input_{field_key}"
            if ss_key in st.session_state:
                if field_type != "selectbox":
                    kwargs["value"] = st.session_state[ss_key]
                kwargs["disabled"] = True

        # 4) Chat input is special-cased
        if field_type == "chat_input":
            handle_chat_input(
                field_key, kwargs, user_input, phase_name, phases, system_prompt
            )
            continue

        # 5) Render with a slightly bolder label style
        with stylable_container(
            key=f"st_label_{phase_name}_{field_key}_{id(field)}",
            css_styles="""
                label p { font-weight: 600; font-size: 16px; }
                div[role="radiogroup"] label p { font-weight: unset !important; font-size: unset !important; }
            """,
        ):
            user_input[field_key] = my_input_function(**kwargs)


# ---------------------------------------------------------------------------
# LLM execution (delegates to family-specific handler)
# ---------------------------------------------------------------------------
def execute_llm_completions(
    SYSTEM_PROMPT: str,
    selected_llm: str,
    phase_instructions: str,
    user_prompt: str,
    image_urls: List[str] | None = None,
):
    """
    Build the context pack and call the appropriate model family handler
    (as registered in HANDLERS). Returns (response_text, execution_price_float).
    """
    if selected_llm not in LLM_CONFIG:
        raise ValueError(f"Selected model '{selected_llm}' not found in configuration.")

    model_config = LLM_CONFIG[selected_llm].copy()

    # Apply overrides from the app config (temperature, max_tokens, etc.)
    if "llm_config" in st.session_state:
        # sidebar-driven controls can update live
        model_config.update(st.session_state["llm_config"])
    if "LLM_CONFIG_OVERRIDE" in globals() and globals()["LLM_CONFIG_OVERRIDE"]:
        model_config.update(globals()["LLM_CONFIG_OVERRIDE"])

    # Build context for handler
    chat_history = st.session_state.get("chat_history", [])
    context = {
        "SYSTEM_PROMPT": SYSTEM_PROMPT,
        "phase_instructions": phase_instructions,
        "user_prompt": user_prompt,
        "supports_image": model_config.get("supports_image", False),
        "image_urls": image_urls or [],
        "model": model_config["model"],
        "max_tokens": model_config.get("max_tokens", 1000),
        "temperature": model_config.get("temperature", 1.0),
        "top_p": model_config.get("top_p", 1.0),
        "frequency_penalty": model_config.get("frequency_penalty", 0.0),
        "presence_penalty": model_config.get("presence_penalty", 0.0),
        "price_input_token_1M": model_config.get("price_input_token_1M", 0),
        "price_output_token_1M": model_config.get("price_output_token_1M", 0),
        "TOTAL_PRICE": 0,
        "chat_history": chat_history,
        # RAG fields (safe defaults)
        "RAG_IMPLEMENTATION": False,
        "file_path": None,
    }

    # If this app declared PUBLISHED=True, ensure stability (cap temperature).
    if "PUBLISHED" in globals() and globals()["PUBLISHED"]:
        context["temperature"] = min(context["temperature"], 0.5)

    family = model_config["family"]
    handler = HANDLERS.get(family)
    if not handler:
        raise NotImplementedError(f"No handler implemented for model family '{family}'")

    try:
        result = handler(context)  # must return (response:str, execution_price:float)
        store_llm_completions(context, result)
        return result
    except Exception as e:
        raise RuntimeError(f"Error in handling the LLM request: {e}")


# ---------------------------------------------------------------------------
# Run logging
# ---------------------------------------------------------------------------
def store_llm_completions(context: Dict[str, Any], result: Any) -> bool | None:
    """
    Persist LLM completions via the configured storage provider.
    Defensive programming to avoid user-visible crashes when storage is not configured.
    """
    try:
        storage = StorageManager.get_storage()

        if not isinstance(result, tuple) or len(result) != 2:
            raise ValueError(
                "Handler must return (response:str, execution_price:float)"
            )

        response, execution_price = result
        for k in ("phase_instructions", "user_prompt", "model"):
            if k not in context:
                raise KeyError(f"Missing required context field: {k}")

        new_row = pd.DataFrame(
            {
                "timestamp": [datetime.now()],
                "APP_TITLE": [globals().get("APP_TITLE", "Unknown App")],
                "Phase Instructions": [str(context["phase_instructions"])],
                "User Prompt": [str(context["user_prompt"])],
                "LLM Response": [str(response)],
                "Run Cost": [float(execution_price)],
                "model": [str(context["model"])],
            }
        )
        storage.post_runs_data(new_row)
        return True

    except Exception as e:
        # Visible in the app, plus prints to server logs
        st.error(f"Error in store_llm_completions: {str(e)}")
        print(f"Error in store_llm_completions: {str(e)}")
        return None


# ---------------------------------------------------------------------------
# Prompt selection & formatting
# ---------------------------------------------------------------------------
def prompt_conditionals(
    user_input: Dict[str, Any], phase_name: str, phases: Dict[str, Any]
) -> str:
    """
    Stitch the final prompt by collecting all prompt blocks whose conditions match.
    If string, use directly. If list, evaluate .condition on each block.
    """
    phase = phases[phase_name]
    prm = phase["user_prompt"]
    if isinstance(prm, str):
        return prm

    chosen: List[str] = []
    for item in prm:
        condition_clause = item.get("condition", {})
        if evaluate_conditions(user_input, condition_clause):
            chosen.append(item["prompt"])
    return "\n".join(chosen)


def format_user_prompt(
    prompt: str, user_input: Dict[str, Any], phase_name: str, phases: Dict[str, Any]
) -> str:
    """
    Format a prompt template with user_input. Supports Chat Input fields by injecting
    full message history for that field. Defensive against missing keys.
    """
    try:
        # Allow conditional prompt building first
        prompt = prompt_conditionals(user_input, phase_name, phases)

        # Field type map so we can detect chat fields
        field_types = {
            k: cfg.get("type") for k, cfg in phases[phase_name]["fields"].items()
        }

        # Prepare format dict
        fmt: Dict[str, Any] = {}
        for key in re.findall(r"{(\w+)}", prompt or ""):
            if field_types.get(key) == "chat_input":
                messages = st.session_state.get(f"messages_{key}", [])
                fmt[key] = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
            else:
                fmt[key] = user_input.get(key, "") or ""

        return (prompt or "").format(**fmt)

    except Exception as e:
        # Do not fail the app because formatting failed; return raw prompt.
        print(f"format_user_prompt error: {e}")
        return prompt or ""


# ---------------------------------------------------------------------------
# Session state helpers
# ---------------------------------------------------------------------------
def st_store(value: Any, phase_name: str, phase_key: str, field_key: str = "") -> None:
    """
    Store a value in session state under a namespaced key.
    """
    key = (
        f"{phase_name}_{field_key}_{phase_key}"
        if field_key
        else f"{phase_name}_{phase_key}"
    )
    st.session_state[key] = value


# ---------------------------------------------------------------------------
# Scoring helpers (optional phases)
# ---------------------------------------------------------------------------
def build_scoring_instructions(rubric: str) -> str:
    return (
        "Please score the user's previous response based on the following rubric:\n"
        f"{rubric}\n\n"
        "Please output your response as JSON, using this format: "
        '\'{{ "[criteria 1]": "[score 1]", "[criteria 2]": "[score 2]", "total": "[total score]" }}\''
    )


def extract_score(text: str) -> int:
    m = re.search(r'"total":\s*"?(\d+)"?', text or "")
    return int(m.group(1)) if m else 0


def check_score(PHASES: Dict[str, Any], PHASE_NAME: str) -> bool:
    score = st.session_state.get(f"{PHASE_NAME}_ai_score", 0)
    try:
        ok = score >= PHASES[PHASE_NAME]["minimum_score"]
        st.session_state[f"{PHASE_NAME}_phase_status"] = bool(ok)
        return bool(ok)
    except Exception:
        st.session_state[f"{PHASE_NAME}_phase_status"] = False
        return False


# ---------------------------------------------------------------------------
# Phase control helpers
# ---------------------------------------------------------------------------
def skip_phase(
    PHASE_NAME: str,
    phases: Dict[str, Any],
    user_input: Dict[str, Any],
    No_Submit: bool = False,
) -> None:
    for field_key in phases[PHASE_NAME]["fields"]:
        st_store(user_input.get(field_key, ""), PHASE_NAME, "user_input", field_key)
    if not No_Submit:
        st.session_state[f"{PHASE_NAME}_ai_response"] = "This phase was skipped."
    st.session_state[f"{PHASE_NAME}_phase_status"] = True
    st.session_state["CURRENT_PHASE"] = min(
        st.session_state["CURRENT_PHASE"] + 1, len(phases) - 1
    )


def celebration() -> None:
    rain(emoji="🥳", font_size=54, falling_speed=5, animation_length=1)


# ---------------------------------------------------------------------------
# Image extraction helpers
# ---------------------------------------------------------------------------
def find_image_urls(user_input: Dict[str, Any], fields: Dict[str, Any]) -> List[str]:
    """
    Extract and base64-encode uploaded images as data URLs for multimodal models.
    Enforces MAX_FILES and MAX_BYTES to keep deployments stable on Community cloud.
    """
    image_urls: List[str] = []
    seen_files = 0

    for key, cfg in fields.items():
        # Skip decorative fields (markdown labels, etc.)
        if cfg.get("decorative"):
            continue

        # Static image by URL/path
        if "image" in cfg and cfg["image"]:
            image_urls.append(cfg["image"])

        # File uploader inputs
        if cfg.get("type") == "file_uploader":
            uploaded = user_input.get(key)
            if not uploaded:
                continue
            files = uploaded if isinstance(uploaded, list) else [uploaded]

            # Enforce count limit
            if seen_files + len(files) > MAX_FILES:
                st.error(f"Too many files. Max {MAX_FILES}. Extra files ignored.")
                files = files[: max(0, MAX_FILES - seen_files)]

            for f in files:
                if not f:
                    continue
                size = getattr(f, "size", None)
                if size is not None and size > MAX_BYTES:
                    st.error(
                        f"{getattr(f, 'name', 'file')} is larger than {MAX_BYTES // (1024 * 1024)} MB and was skipped."
                    )
                    continue

                # Determine mime type; default to octet-stream if we can't guess
                mime, _ = mimetypes.guess_type(getattr(f, "name", "file"))
                mime = mime or "application/octet-stream"
                content = f.read()
                b64 = base64.b64encode(content).decode("utf-8")
                image_urls.append(f"data:{mime};base64,{b64}")
                seen_files += 1

    return image_urls


# ---------------------------------------------------------------------------
# Chat helpers
# ---------------------------------------------------------------------------
def handle_chat_history(
    user_input: Any,
    ai_response: str,
    phase_instructions: str | None = None,
    image_urls: List[str] | None = None,
) -> None:
    entry = {"user": user_input, "assistant": ai_response}
    if phase_instructions:
        entry["assistant_instructions"] = phase_instructions
    if image_urls:
        entry["app_images"] = image_urls
    hist = st.session_state.setdefault("chat_history", [])
    hist.append(entry)
    # Trim history to avoid session bloat
    if len(hist) > 50:
        st.session_state["chat_history"] = hist[-50:]


def handle_chat_input(
    field_key: str,
    kwargs: Dict[str, Any],
    user_input: Dict[str, Any],
    phase_name: str,
    phases: Dict[str, Any],
    system_prompt: str,
) -> None:
    selected_llm = st.session_state.get("selected_llm", "openai")
    initial_msg = kwargs.pop("initial_assistant_message", "")
    msg_key = f"messages_{field_key}"

    if msg_key not in st.session_state:
        st.session_state[msg_key] = [{"role": "assistant", "content": initial_msg}]

    phase = phases[phase_name]
    field_cfg = phase["fields"][field_key]
    max_messages = int(field_cfg.get("max_messages", 50))

    # Counter (each exchange is 2 messages)
    current_exchanges = len(st.session_state[msg_key]) // 2
    remaining = max_messages - current_exchanges
    st.write(f"Messages remaining: {remaining}/{max_messages}")

    # Show transcript so far
    for m in st.session_state[msg_key]:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    # Stop if limit reached
    if len(st.session_state[msg_key]) >= max_messages * 2:
        st.info(
            f"Maximum message limit ({max_messages}) reached. Please proceed to the next phase."
        )
        return

    # Accept new user input
    user_input[field_key] = st.chat_input(**kwargs)
    if user_input[field_key]:
        # Clear per-phase old markers if any
        for suffix in (
            "ai_response",
            "ai_score_debug",
            "error_message",
            "warning_message",
        ):
            st.session_state.pop(f"{phase_name}_{suffix}", None)

        # Display user's message
        with st.chat_message("user"):
            st.markdown(user_input[field_key])
        st.session_state[msg_key].append(
            {"role": "user", "content": user_input[field_key]}
        )

        # LLM response
        phase_instructions = phase.get("phase_instructions", "")
        ai_response, price = execute_llm_completions(
            system_prompt, selected_llm, phase_instructions, user_input[field_key]
        )
        st.session_state["TOTAL_PRICE"] = st.session_state.get(
            "TOTAL_PRICE", 0.0
        ) + float(price)

        with st.chat_message("assistant"):
            st.markdown(ai_response)
        st.session_state[msg_key].append({"role": "assistant", "content": ai_response})

        handle_chat_history(user_input[field_key], ai_response, phase_instructions)
        st.rerun()


# ---------------------------------------------------------------------------
# Submission pipeline
# ---------------------------------------------------------------------------
def handle_submission(
    PHASE_NAME: str,
    PHASE_DICT: Dict[str, Any],
    fields: Dict[str, Any],
    user_input: Dict[str, Any],
    formatted_user_prompt: str,
    selected_llm: str,
    SYSTEM_PROMPT: str,
    PHASES: Dict[str, Any],
) -> bool:
    """
    Finalize a phase: persist inputs, execute LLM (and optionally score),
    handle history and advance state machine.
    """
    for fk in fields:
        st_store(user_input.get(fk, ""), PHASE_NAME, "user_input", fk)

    phase_instructions = PHASE_DICT.get("phase_instructions", "")
    image_urls = find_image_urls(user_input, PHASE_DICT.get("fields", {}))

    if PHASE_DICT.get("ai_response", True):
        if PHASE_DICT.get("scored_phase", False):
            if "rubric" not in PHASE_DICT:
                st_store(
                    "You need to include a rubric for a scored phase",
                    PHASE_NAME,
                    "error_message",
                )
                return False

            # 1) Feedback
            ai_feedback, price1 = execute_llm_completions(
                SYSTEM_PROMPT,
                selected_llm,
                phase_instructions,
                formatted_user_prompt,
                image_urls,
            )
            st.session_state["TOTAL_PRICE"] = st.session_state.get(
                "TOTAL_PRICE", 0.0
            ) + float(price1)
            st.info(body=ai_feedback, icon="🤖")
            st_store(ai_feedback, PHASE_NAME, "ai_response")

            # 2) Score
            scoring_instructions = build_scoring_instructions(PHASE_DICT["rubric"])
            ai_score, price2 = execute_llm_completions(
                "You review the previous conversation and provide a score based on a rubric. You always provide your output in JSON format.",
                selected_llm,
                scoring_instructions,
                formatted_user_prompt,
            )
            st.session_state["TOTAL_PRICE"] += float(price2)
            st.info(ai_score, icon="🤖")
            st_store(ai_score, PHASE_NAME, "ai_score_debug")
            st_store(extract_score(ai_score), PHASE_NAME, "ai_score")

            handle_chat_history(
                formatted_user_prompt, ai_feedback, phase_instructions, image_urls
            )

            if check_score(PHASES, PHASE_NAME):
                st.session_state["CURRENT_PHASE"] = min(
                    st.session_state["CURRENT_PHASE"] + 1, len(PHASES) - 1
                )
                st.session_state[f"{PHASE_NAME}_warning_message"] = None
                st.session_state[f"{PHASE_NAME}_error_message"] = None
                st.session_state[f"{PHASE_NAME}_phase_completed"] = True
                return True
            else:
                st.session_state.pop(f"messages_{PHASE_NAME}", None)
                st_store(
                    "You haven't passed. Please try again.",
                    PHASE_NAME,
                    "warning_message",
                )
                return False

        else:
            # Simple non-scored phase
            ai_feedback, price = execute_llm_completions(
                SYSTEM_PROMPT,
                selected_llm,
                phase_instructions,
                formatted_user_prompt,
                image_urls,
            )
            st_store(ai_feedback, PHASE_NAME, "ai_response")
            st.session_state["TOTAL_PRICE"] = st.session_state.get(
                "TOTAL_PRICE", 0.0
            ) + float(price)

            handle_chat_history(
                formatted_user_prompt, ai_feedback, phase_instructions, image_urls
            )
            st.session_state["CURRENT_PHASE"] = min(
                st.session_state["CURRENT_PHASE"] + 1, len(PHASES) - 1
            )
            st.session_state[f"{PHASE_NAME}_phase_completed"] = True
            return True

    else:
        # No AI call; display custom response instead (animated)
        res_box = st.info(body="", icon="🤖")
        msg = PHASE_DICT.get("custom_response", "") or ""
        msg = format_user_prompt(msg, user_input, PHASE_NAME, PHASES)
        acc = ""
        for ch in msg:
            acc += ch
            res_box.info(body=acc, icon="🤖")

        st.session_state[f"{PHASE_NAME}_ai_response"] = msg
        handle_chat_history(formatted_user_prompt, msg, phase_instructions, image_urls)
        st.session_state["CURRENT_PHASE"] = min(
            st.session_state["CURRENT_PHASE"] + 1, len(PHASES) - 1
        )
        st.session_state[f"{PHASE_NAME}_phase_completed"] = True
        return True


# ---------------------------------------------------------------------------
# Main app runner
# ---------------------------------------------------------------------------
def main(config: Dict[str, Any]) -> None:
    """
    Entrypoint for all micro-apps. Accepts a `config` (typically `globals()` from
    the app file) and sets up Streamlit UI, model selection, prompts, and run loop.
    """
    PAGE_CONFIG = config.get("PAGE_CONFIG", {})
    SIDEBAR_HIDDEN = config.get("SIDEBAR_HIDDEN", True)
    DISPLAY_COST = config.get("DISPLAY_COST", False)

    # Expose globals used by other helpers
    global APP_TITLE
    APP_TITLE = config.get("APP_TITLE", "Default Title")
    APP_INTRO = config.get("APP_INTRO", "")
    APP_HOW_IT_WORKS = config.get("APP_HOW_IT_WORKS", "")
    SHARED_ASSET = config.get("SHARED_ASSET", None)
    HTML_BUTTON = config.get("HTML_BUTTON", None)
    PHASES = config.get("PHASES", {"phase1": {"name": "default phase"}})
    COMPLETION_MESSAGE = config.get(
        "COMPLETION_MESSAGE", "Process completed successfully."
    )
    COMPLETION_CELEBRATION = config.get("COMPLETION_CELEBRATION", False)
    global LLM_CONFIG_OVERRIDE
    LLM_CONFIG_OVERRIDE = config.get("LLM_CONFIG_OVERRIDE", {})
    global GSHEETS_URL_OVERRIDE
    GSHEETS_URL_OVERRIDE = config.get("GSHEETS_URL_OVERRIDE", None)
    global GSHEETS_WORKSHEET_OVERRIDE
    GSHEETS_WORKSHEET_OVERRIDE = config.get("GSHEETS_WORKSHEET_OVERRIDE", "Sheet1")
    PREFERRED_LLM = config.get("PREFERRED_LLM", "openai")
    SYSTEM_PROMPT = config.get("SYSTEM_PROMPT", "")

    if PAGE_CONFIG:
        st.set_page_config(
            page_title=PAGE_CONFIG.get("page_title", "AI MicroApps"),
            page_icon=PAGE_CONFIG.get("page_icon", "🤖"),
            layout=PAGE_CONFIG.get("layout", "wide"),
            initial_sidebar_state=PAGE_CONFIG.get("initial_sidebar_state", "collapsed"),
        )

    # Initialize storage backend (no-op or real)
    StorageManager.initialize(config)

    # Optionally hide sidebar controls (cleaner prod look)
    if SIDEBAR_HIDDEN:
        st.markdown(
            """
            <style>
                [data-testid="stSidebar"] {display: none;}
                [data-testid="stSidebarCollapsedControl"] {display: none;}
            </style>
            """,
            unsafe_allow_html=True,
        )

    # Template identity (used to reset state when app changes)
    selected_template = APP_TITLE
    if (
        "template" not in st.session_state
        or st.session_state.template != selected_template
    ):
        st.session_state.template = selected_template
        st.query_params["template"] = selected_template

        # Reset almost everything for a clean slate
        keep = {"template"}
        for k in list(st.session_state.keys()):
            if k not in keep:
                del st.session_state[k]

        st.session_state["additional_prompt"] = ""
        st.session_state["chat_history"] = []
        st.session_state["CURRENT_PHASE"] = 0
        st.session_state["TOTAL_PRICE"] = 0.0
        st.rerun()

    user_input: Dict[str, Any] = {}
    if "TOTAL_PRICE" not in st.session_state:
        st.session_state["TOTAL_PRICE"] = 0.0

    # ---------------- Sidebar (model chooser + cost + history) ----------------
    with st.sidebar:
        llm_options = list(LLM_CONFIG.keys())
        llm_index = (
            llm_options.index(PREFERRED_LLM) if PREFERRED_LLM in llm_options else 0
        )
        selected_llm = st.selectbox(
            "Select Language Model",
            options=llm_options,
            index=llm_index,
            key="selected_llm",
        )

        # Seed live llm_config for sliders (copy base, then apply overrides)
        initial_config = LLM_CONFIG[selected_llm].copy()
        if LLM_CONFIG_OVERRIDE:
            initial_config.update(LLM_CONFIG_OVERRIDE)

        st.session_state["llm_config"] = {
            "model": initial_config["model"],
            "temperature": st.slider(
                "Temperature",
                0.0,
                1.0,
                float(initial_config.get("temperature", 1.0)),
                0.01,
            ),
            "max_tokens": st.slider(
                "Max Tokens", 50, 10000, int(initial_config.get("max_tokens", 1000)), 50
            ),
            "top_p": st.slider(
                "Top P", 0.0, 1.0, float(initial_config.get("top_p", 1.0)), 0.1
            ),
            "frequency_penalty": st.slider(
                "Frequency Penalty",
                0.0,
                1.0,
                float(initial_config.get("frequency_penalty", 0.0)),
                0.01,
            ),
            "presence_penalty": st.slider(
                "Presence Penalty",
                0.0,
                1.0,
                float(initial_config.get("presence_penalty", 0.0)),
                0.01,
            ),
            "price_input_token_1M": st.number_input(
                "Input Token Price 1M",
                value=float(initial_config.get("price_input_token_1M", 0)),
            ),
            "price_output_token_1M": st.number_input(
                "Output Token Price 1M",
                value=float(initial_config.get("price_output_token_1M", 0)),
            ),
        }

        if DISPLAY_COST:
            st.write("Price: ${:.6f}".format(st.session_state.get("TOTAL_PRICE", 0.0)))

        # Chat history preview
        st.subheader("Chat History")
        for history in st.session_state.get("chat_history", []):
            if "assistant_instructions" in history:
                with st.chat_message("assistant_instructions"):
                    st.write(history["assistant_instructions"])
            with st.chat_message("user"):
                st.write(history["user"])
                for img in history.get("app_images", []):
                    st.image(img)
            with st.chat_message("assistant"):
                st.write(history["assistant"])

    # ---------------- Main area ----------------
    if "CURRENT_PHASE" not in st.session_state:
        st.session_state["CURRENT_PHASE"] = 0

    st.title(APP_TITLE)
    if APP_INTRO:
        st.markdown(APP_INTRO)

    if APP_HOW_IT_WORKS:
        with st.expander("Learn how this works", expanded=False):
            st.markdown(APP_HOW_IT_WORKS)

    if SHARED_ASSET:
        with open(SHARED_ASSET["path"], "rb") as fh:
            st.download_button(
                label=SHARED_ASSET["button_text"],
                data=fh,
                file_name=SHARED_ASSET["name"],
                mime="application/octet-stream",
            )

    if HTML_BUTTON:
        st.link_button(label=HTML_BUTTON["button_text"], url=HTML_BUTTON["url"])

    # Phases run loop
    i = 0
    while i <= st.session_state["CURRENT_PHASE"]:
        PHASE_NAME = list(PHASES.keys())[i]
        PHASE_DICT = PHASES[PHASE_NAME]
        fields = PHASE_DICT["fields"]

        st.write(f"#### Phase {i + 1}: {PHASE_DICT['name']}")
        build_field(PHASE_NAME, fields, user_input, PHASES, SYSTEM_PROMPT)

        # Compose final user prompt (show editor if allowed)
        user_prompt_template = PHASE_DICT.get("user_prompt", "")
        read_only = PHASE_DICT.get("read_only_prompt", False)
        if PHASE_DICT.get("show_prompt", False):
            with st.expander("View/edit full prompt"):
                formatted_user_prompt = st.text_area(
                    label="Prompt",
                    height=100,
                    max_chars=50000,
                    value=format_user_prompt(
                        user_prompt_template, user_input, PHASE_NAME, PHASES
                    ),
                    disabled=read_only,
                )
        else:
            formatted_user_prompt = format_user_prompt(
                user_prompt_template, user_input, PHASE_NAME, PHASES
            )

        # Auto-advance phases without submission if configured
        phase_status_key = f"{PHASE_NAME}_phase_status"
        if PHASE_DICT.get("no_submission", False) and not st.session_state.get(
            phase_status_key, False
        ):
            st.session_state[phase_status_key] = True
            st.session_state["CURRENT_PHASE"] = min(
                st.session_state["CURRENT_PHASE"] + 1, len(PHASES) - 1
            )
            st.session_state[f"{PHASE_NAME}_phase_completed"] = True
            st.rerun()

        if phase_status_key not in st.session_state:
            st.session_state[phase_status_key] = False

        has_chat_input = any(
            v.get("type") == "chat_input" for v in PHASE_DICT["fields"].values()
        )
        container_class = _bottom.container() if has_chat_input else st.container()
        default_label = "End Chat" if has_chat_input else "Submit"

        submit_button = False
        skip_button = False
        if not st.session_state.get(f"{PHASE_NAME}_phase_completed", False):
            with container_class:
                c1, c2 = st.columns(2)
                with c1:
                    submit_button = st.button(
                        PHASE_DICT.get("button_label", default_label),
                        type="primary",
                        key=f"submit_{i}",
                    )
                with c2:
                    if PHASE_DICT.get("allow_skip", False):
                        skip_button = st.button("Skip Question", key=f"skip_{i}")

        # Show any stored outputs/messages
        for suffix, show_fn in (
            ("ai_response", st.info),
            ("ai_score_debug", st.info),
            ("warning_message", st.warning),
            ("error_message", lambda msg: st.error(msg, icon="🚨")),
        ):
            key = f"{PHASE_NAME}_{suffix}"
            if st.session_state.get(key):
                show_fn(st.session_state[key])

        # Revisions (if enabled)
        if PHASE_DICT.get("allow_revisions", False) and st.session_state.get(
            f"{PHASE_NAME}_ai_response"
        ):
            is_last_phase = PHASE_NAME == list(PHASES.keys())[-1]
            is_latest_or_last = i == st.session_state["CURRENT_PHASE"] or (
                i == st.session_state["CURRENT_PHASE"] - 1
                and not st.session_state.get(
                    f"{list(PHASES.keys())[i + 1]}_phase_completed", False
                )
            )
            if (is_latest_or_last or is_last_phase) and not st.session_state.get(
                f"{PHASE_NAME}_skipped", False
            ):
                with st.expander("Revise this response?"):
                    max_revs = int(PHASE_DICT.get("max_revisions", 10))
                    rev_key = f"{PHASE_NAME}_revision_count"
                    if rev_key not in st.session_state:
                        st_store(0, PHASE_NAME, "revision_count")
                    if st.session_state[rev_key] < max_revs:
                        st.session_state["additional_prompt"] = st.text_input(
                            "Enter additional prompt", value="", key=PHASE_NAME
                        )
                        if st.button("Revise", key=f"revise_{i}"):
                            st.session_state[rev_key] += 1
                            formatted = format_user_prompt(
                                user_prompt_template, user_input, PHASE_NAME, PHASES
                            )
                            formatted += st.session_state["additional_prompt"]
                            resp, price = execute_llm_completions(
                                SYSTEM_PROMPT,
                                st.session_state["selected_llm"],
                                PHASE_DICT.get("phase_instructions", ""),
                                formatted,
                            )
                            st.session_state["TOTAL_PRICE"] += float(price)
                            st_store(
                                resp,
                                PHASE_NAME,
                                f"ai_response_revision_{st.session_state[rev_key]}",
                            )
                            handle_chat_history(
                                formatted,
                                resp,
                                PHASE_DICT.get("phase_instructions", ""),
                                [],
                            )
                            st.rerun()
                    else:
                        st.warning("Revision limits exceeded")

        if submit_button:
            handle_submission(
                PHASE_NAME,
                PHASE_DICT,
                fields,
                user_input,
                formatted_user_prompt,
                st.session_state["selected_llm"],
                SYSTEM_PROMPT,
                PHASES,
            )
            st.rerun()

        if skip_button:
            skip_phase(PHASE_NAME, PHASES, user_input)
            st.session_state[f"{PHASE_NAME}_phase_completed"] = True
            st.session_state[f"{PHASE_NAME}_skipped"] = True
            st.rerun()

        final_phase = list(PHASES.keys())[-1]
        if (
            st.session_state.get(f"{final_phase}_ai_response")
            and i == st.session_state["CURRENT_PHASE"]
        ):
            st.success(COMPLETION_MESSAGE)
            if COMPLETION_CELEBRATION:
                celebration()

        i = min(i + 1, len(PHASES))

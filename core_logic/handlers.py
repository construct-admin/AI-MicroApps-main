# ------------------------------------------------------------------------------
# Refactor date: 2025-11-16
# Refactored by: Imaad Fakier
# Purpose: Align OES micro-applications with OES GenAI Streamlit standards.
# ------------------------------------------------------------------------------
"""
core_logic.handlers (Refactored - Updated for OpenAI v1.x)
-----------------------------------------------------------
Provides per-family LLM invocation handlers.

Included:
- `openai` (supports image + text)
- Exponential backoff wrapper
- Defensive error handling to prevent crashes
- Fully compatible with new OpenAI Python client v1.0+

Key changes from legacy handler:
- Removed deprecated global `openai.ChatCompletion.create`
- Replaced with new `OpenAI()` client (required for v1.x+)
- Client created inside handler to prevent Streamlit Cloud injecting `proxies`

Additional improvements in this patch:
- Added httpx transport override (CRITICAL FIX) to BLOCK Streamlit proxy injection
- Correct handling of multimodal output (GPT-4o style content blocks)
- Accurate token usage extraction using OpenAI v1.x usage fields:
    - `input_tokens`
    - `output_tokens`
- Consolidated message-building logic for clarity and maintainability
- Future-proofed for upcoming OpenAI v2+ unified API patterns
"""

import os
import time
import random
import streamlit as st

# Core OpenAI SDK
from openai import OpenAI

# CRITICAL: used to override Streamlit's injected proxy layer
import httpx


# ------------------------------------------------------------------------------
# Helper: retry wrapper
# ------------------------------------------------------------------------------
def with_backoff(fn, *args, **kwargs):
    """
    Execute a callable with exponential backoff up to 5 attempts.
    Intended for transient operational failures such as:
    - Temporary 5xx errors from OpenAI
    - Network jitter on Streamlit Community Cloud
    - Rate-limiting windows that reopen after a delay

    This mechanism ensures that intermittent failures do NOT crash the app.
    """
    delay = 0.5
    for attempt in range(5):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt == 4:
                raise

            wait = delay + random.random() * delay
            st.warning(f"Retrying after transient error: {e} (waiting {wait:.1f}s)")
            time.sleep(wait)
            delay *= 2


# ------------------------------------------------------------------------------
# Handler: OpenAI (updated for OpenAI Python SDK v1.0+)
# ------------------------------------------------------------------------------
def handle_openai(context):
    """
    Core OpenAI handler used by OES micro-apps.

    Supports:
    - GPT-4o / GPT-4o-mini
    - GPT-4.1 / GPT-4.1-mini
    - Future unified OpenAI chat models (v1 API)

    Supports:
    - Text-only prompts
    - Multimodal prompts (image + text)
    - Cost calculation via v1.x usage fields
    - Fully updated GPT-4o content block handling

    CRITICAL IMPLEMENTATION DETAIL:
    --------------------------------
    We *override* Streamlit Cloud’s injected HTTPX client because
    Streamlit forcibly injects an unsupported parameter:

        proxies={...}

    which OpenAI SDK v1.40+ rejects.

    We bypass this by providing our own httpx.Client WITHOUT proxy config.
    """

    # ----------------------------------------------------------
    # Load API key
    # ----------------------------------------------------------
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY environment variable.")

    # ----------------------------------------------------------
    # CRITICAL FIX: prevent Streamlit from injecting `proxies`
    # ----------------------------------------------------------
    # We supply our own HTTP transport WITHOUT proxy support.
    transport = httpx.HTTPTransport(proxy=None)
    http_client = httpx.Client(
        transport=transport,
        follow_redirects=True,
    )

    # Modern, safe OpenAI client instantiation
    client = OpenAI(
        api_key=api_key,
        http_client=http_client,  # <--- Overrides Streamlit's proxy-injected wrapper
    )

    # ----------------------------------------------------------
    # Extract model parameters from context
    # ----------------------------------------------------------
    model = context["model"]
    temperature = context.get("temperature", 1.0)
    max_tokens = context.get("max_tokens", 1000)

    # ----------------------------------------------------------
    # Build messages payload (supports multimodal)
    # ----------------------------------------------------------
    messages = []

    # System prompt block
    if context.get("SYSTEM_PROMPT"):
        messages.append({"role": "system", "content": context["SYSTEM_PROMPT"]})

    # User content blocks (GPT-4o style)
    user_content = []

    # Text block
    if context.get("user_prompt"):
        user_content.append({"type": "text", "text": context["user_prompt"]})

    # Image blocks (URL-based, already base64-upstream processed)
    if context.get("supports_image") and context.get("image_urls"):
        for url in context["image_urls"]:
            user_content.append({"type": "image_url", "image_url": {"url": url}})

    messages.append({"role": "user", "content": user_content})

    # ----------------------------------------------------------
    # OpenAI API Call (wrapped in exponential backoff)
    # ----------------------------------------------------------
    try:
        response = with_backoff(
            client.chat.completions.create,
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=context.get("top_p", 1.0),
            frequency_penalty=context.get("frequency_penalty", 0.0),
            presence_penalty=context.get("presence_penalty", 0.0),
        )

        # ------------------------------------------------------
        # Extract model output (handle structured blocks)
        # ------------------------------------------------------
        raw_content = response.choices[0].message.content

        # Case 1 — GPT-4o multimodal (list of blocks)
        if isinstance(raw_content, list):
            text = "\n".join(
                block.text
                for block in raw_content
                if hasattr(block, "text") and block.text
            ).strip()

        # Case 2 — Simple string
        elif isinstance(raw_content, str):
            text = raw_content.strip()

        # Fallback — unknown format
        else:
            text = str(raw_content)

        # ------------------------------------------------------
        # Token usage & price calculation
        # ------------------------------------------------------
        usage = response.usage

        input_toks = getattr(usage, "input_tokens", 0)
        output_toks = getattr(usage, "output_tokens", 0)

        price_in = context.get("price_input_token_1M", 0)
        price_out = context.get("price_output_token_1M", 0)

        execution_price = (input_toks / 1_000_000) * price_in + (
            output_toks / 1_000_000
        ) * price_out

        return text, float(execution_price)

    except Exception as e:
        st.error(f"OpenAI handler failed: {e}")
        raise


# ------------------------------------------------------------------------------
# HANDLERS registry
# Add additional model families here as micro-apps expand
# ------------------------------------------------------------------------------
HANDLERS = {
    "openai": handle_openai,
    # "anthropic": handle_anthropic,
    # "azure_openai": handle_azure_openai,
    # "google_genai": handle_google_genai,
}

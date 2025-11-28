# ------------------------------------------------------------------------------
# Refactor date: 2025-11-16
# Refactored by: Imaad Fakier
# Purpose:
#   Centralised configuration for all LLMs used across OES GenAI micro-apps.
#
# Why this file exists:
#   - Standardises all model metadata in a single source of truth
#   - Ensures each model declares identical structural fields
#   - Enables dynamic UI: price breakdowns, sliders, capabilities (image/text)
#   - Keeps handlers simple (they only rely on `family` + `model`)
#
# Notes:
#   • Every model must define:
#       - family:   Handler family ("openai", "claude", "gemini", "perplexity", "rag")
#       - model:    Provider-specific model name
#       - max_tokens, temperature, top_p, penalties
#       - supports_image: enables multimodal uploads
#       - price_input_token_1M / price_output_token_1M: USD/M-token pricing
#
#   • Additional fields can be safely added (UI will automatically expose them
#     if included in sidebar controls).
#
#   • Handlers for each family must exist in `core_logic.handlers`.
#
#   • Pricing values are *current as of 2025* but can be overridden in `config.py`
#     via `LLM_CONFIG_OVERRIDE`.
# ------------------------------------------------------------------------------

LLM_CONFIG = {
    # ----------------------------------------------------------------------
    # 🔵 OPENAI (GPT-4o / GPT-4 Turbo / GPT-4o-mini)
    # ----------------------------------------------------------------------
    "gpt-4o-mini": {
        "family": "openai",
        "model": "gpt-4o-mini",
        "max_tokens": 1000,
        "temperature": 1.0,
        "top_p": 1.0,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "supports_image": False,
        "price_input_token_1M": 0.15,
        "price_output_token_1M": 0.60,
    },
    "gpt-4-turbo": {
        "family": "openai",
        "model": "gpt-4-turbo",
        "max_tokens": 1000,
        "temperature": 1.0,
        "top_p": 1.0,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "supports_image": True,
        "price_input_token_1M": 10,
        "price_output_token_1M": 30,
    },
    "gpt-4o": {
        "family": "openai",
        "model": "gpt-4o",
        "max_tokens": 2000,
        "temperature": 1.0,
        "top_p": 1.0,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "supports_image": True,
        "price_input_token_1M": 2.5,
        "price_output_token_1M": 10,
    },
    # ----------------------------------------------------------------------
    # 🟣 RAG (Retrieval-Augmented Generation)
    # Framework-only entry: this model is processed by the RAG handler
    # rather than directly calling OpenAI.
    # ----------------------------------------------------------------------
    "rag-with-gpt-4o": {
        "family": "rag",
        "model": "gpt-4-turbo",  # underlying generator
        "max_tokens": 1000,
        "temperature": 1.0,
        "top_p": 1.0,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "supports_image": True,
        "price_input_token_1M": 10,
        "price_output_token_1M": 30,
    },
    # ----------------------------------------------------------------------
    # 🟡 GOOGLE GEMINI 2.0 FAMILY
    # ----------------------------------------------------------------------
    "gemini-2.0-flash-lite": {
        "family": "gemini",
        "model": "gemini-2.0-flash-lite",
        "max_tokens": 1000,
        "temperature": 1.0,
        "top_p": 0.95,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "supports_image": False,
        "price_input_token_1M": 0.15,
        "price_output_token_1M": 0.60,
    },
    "gemini-2.0-flash": {
        "family": "gemini",
        "model": "gemini-2.0-flash",
        "max_tokens": 1000,
        "temperature": 1.0,
        "top_p": 0.95,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "supports_image": True,
        "price_input_token_1M": 2.5,
        "price_output_token_1M": 10.00,
    },
    # ----------------------------------------------------------------------
    # 🟠 ANTHROPIC CLAUDE 3.5 / OPUS / HAIKU
    # ----------------------------------------------------------------------
    "claude-3.5-sonnet": {
        "family": "claude",
        "model": "claude-3-5-sonnet-latest",
        "max_tokens": 1000,
        "temperature": 1.0,
        "top_p": 1.0,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "supports_image": True,
        "price_input_token_1M": 3,
        "price_output_token_1M": 15,
    },
    "claude-opus": {
        "family": "claude",
        "model": "claude-3-opus-latest",
        "max_tokens": 1000,
        "temperature": 1.0,
        "top_p": 1.0,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "supports_image": True,
        "price_input_token_1M": 15,
        "price_output_token_1M": 75,
    },
    "claude-3.5-haiku": {
        "family": "claude",
        "model": "claude-3-5-haiku-latest",
        "max_tokens": 1000,
        "temperature": 1.0,
        "top_p": 1.0,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "supports_image": False,
        "price_input_token_1M": 1,
        "price_output_token_1M": 5,
    },
    # ----------------------------------------------------------------------
    # 🔵 PERPLEXITY – SONAR / LLAMA 3.1 MODELS
    # These rely on a separate "perplexity" handler, not OpenAI.
    # ----------------------------------------------------------------------
    "llama-3.1-sonar-small-128k-chat": {
        "family": "perplexity",
        "model": "llama-3.1-sonar-small-128k-chat",
        "max_tokens": 1000,
        "temperature": 1.0,
        "top_p": 1.0,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "supports_image": False,
        "price_input_token_1M": 0.20,
        "price_output_token_1M": 0.20,
    },
    "llama-3.1-sonar-small-128k-online": {
        "family": "perplexity",
        "model": "llama-3.1-sonar-small-128k-online",
        "max_tokens": 1000,
        "temperature": 1.0,
        "top_p": 1.0,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "supports_image": False,
        "price_input_token_1M": 0.20,
        "price_output_token_1M": 0.20,
    },
    "llama-3.1-sonar-large-128k-chat": {
        "family": "perplexity",
        "model": "llama-3.1-sonar-large-128k-chat",
        "max_tokens": 1000,
        "temperature": 1.0,
        "top_p": 1.0,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "supports_image": False,
        "price_input_token_1M": 1.0,
        "price_output_token_1M": 1.0,
    },
    "llama-3.1-sonar-large-128k-online": {
        "family": "perplexity",
        "model": "llama-3.1-sonar-large-128k-online",
        "max_tokens": 1000,
        "temperature": 1.0,
        "top_p": 1.0,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "supports_image": False,
        "price_input_token_1M": 1.0,
        "price_output_token_1M": 1.0,
    },
    "llama-3.1-8b-instruct": {
        "family": "perplexity",
        "model": "llama-3.1-8b-instruct",
        "max_tokens": 1000,
        "temperature": 1.0,
        "top_p": 1.0,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "supports_image": False,
        "price_input_token_1M": 0.20,
        "price_output_token_1M": 0.20,
    },
    "llama-3.1-70b-instruct": {
        "family": "perplexity",
        "model": "llama-3.1-70b-instruct",
        "max_tokens": 1000,
        "temperature": 1.0,
        "top_p": 1.0,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "supports_image": False,
        "price_input_token_1M": 1.0,
        "price_output_token_1M": 1.0,
    },
}

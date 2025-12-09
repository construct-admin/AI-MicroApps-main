# ------------------------------------------------------------------------------
# File: kb.py
# Refactor date: 2025-12-08
# Refactored by: Imaad Fakier
#
# Purpose:
#     Provide a compatibility-safe wrapper around the OpenAI Python SDK’s
#     Vector Store capabilities. This module acts as the “knowledge base layer”
#     for OES GenAI micro-applications, enabling:
#
#         • Creation of vector stores (beta + non-beta support)
#         • Upload of files to vector stores (batch, legacy, fallback modes)
#         • Graceful degradation when the user’s OpenAI SDK lacks VS support
#
# Behaviour:
#     - Zero functional changes from the original version.
#     - All compatibility branches preserved (beta surfaces + legacy surfaces).
#     - Internal helpers preserved exactly; only documentation added.
#
# External API Requirements:
#     - openai >= 1.0.0 (Vector Stores introduced), but the module safely handles
#       older or partially-compatible versions.
#
# Notes:
#     - `ensure_client()` is the only entry point required to initialize the SDK.
#     - This module is intentionally lightweight and side-effect free.
# ------------------------------------------------------------------------------

from io import BytesIO
from typing import Dict, Any
from openai import OpenAI
import os


# ==============================================================================
# Client Initialization (Patched for OpenAI v1.x + Streamlit Cloud compatibility)
# ==============================================================================

import httpx  # CRITICAL: needed to override Streamlit’s proxy-injected HTTP layer


def ensure_client(api_key: str) -> OpenAI:
    """
    Create a fully configured OpenAI client instance.

    Parameters:
        api_key (str):
            User-provided secret. Must not be empty.

    Returns:
        OpenAI:
            Authenticated OpenAI client.

    Raises:
        ValueError:
            If api_key is missing or empty.

    CRITICAL FIX:
    -------------
    Streamlit Cloud *injects* a hidden proxies={} argument into all httpx clients.
    The OpenAI Python SDK v1.x rejects this and raises:

        TypeError: Client.__init__() got an unexpected keyword argument 'proxies'

    To bypass this, we manually create an httpx.Client with NO proxy layer
    and explicitly pass it to OpenAI(), which avoids Streamlit’s wrapper.
    """
    if not api_key:
        raise ValueError("Missing OpenAI API key")

    os.environ["OPENAI_API_KEY"] = api_key

    # ----------------------------------------------------------
    # BLOCK Streamlit’s proxy layer (CRITICAL)
    # ----------------------------------------------------------
    transport = httpx.HTTPTransport(proxy=None)
    http_client = httpx.Client(
        transport=transport,
        follow_redirects=True,
    )

    # ----------------------------------------------------------
    # Safe OpenAI client instantiation (v1.x compliant)
    # ----------------------------------------------------------
    client = OpenAI(
        api_key=api_key,
        http_client=http_client,  # <--- Prevents Streamlit from injecting proxies
    )

    return client


# ==============================================================================
# Compatibility Checks (Public API)
# ==============================================================================


def _has_beta_vs(client: OpenAI) -> bool:
    """
    True if the client exposes the new-style:
        client.beta.vector_stores
    """
    return hasattr(client, "beta") and hasattr(client.beta, "vector_stores")


def _has_vs(client: OpenAI) -> bool:
    """
    True if the client exposes the older non-beta:
        client.vector_stores
    """
    return hasattr(client, "vector_stores")


def vector_store_supported(client: OpenAI) -> bool:
    """
    Return True if *any* Vector Store surface is available.

    This is used by app.py to show/hide the “Upload to Knowledge Base”
    UI elements depending on SDK capability.
    """
    return _has_beta_vs(client) or _has_vs(client)


# ==============================================================================
# Internal Helpers – File Preparation
# ==============================================================================


def _name_stream(stream: BytesIO, filename: str) -> BytesIO:
    """
    Ensure that a BytesIO stream has a `.name` attribute for SDKs that
    expect it. If present, do nothing. Non-fatal on failure.
    """
    try:
        if hasattr(stream, "read") and not getattr(stream, "name", None):
            stream.name = filename
    except Exception:
        pass
    return stream


def _upload_file_object(client: OpenAI, data: BytesIO, filename: str):
    """
    Upload a file object to OpenAI Files, supporting both legacy and modern
    signatures:

        • files.create(file=(filename, file_like), purpose="assistants")
        • files.create(file=file_like, purpose="assistants", filename="...")

    The OpenAI Python SDK has varied across versions, so this helper gracefully
    handles both syntaxes.

    Returns:
        File object returned by OpenAI (with .id attribute).
    """
    data.seek(0)
    data = _name_stream(data, filename)

    try:
        # Preferred for newer-style SDKs
        return client.files.create(file=(filename, data), purpose="assistants")
    except TypeError:
        # Fallback for older-style SDKs
        return client.files.create(file=data, purpose="assistants", filename=filename)


# ==============================================================================
# Vector Store Creation
# ==============================================================================


def create_vector_store(client: OpenAI, name: str = "Canvas Templates / KB") -> str:
    """
    Create a vector store using whichever interface is available in the
    user’s OpenAI SDK version.

    Parameters:
        client (OpenAI): Authenticated SDK client.
        name (str): A display name for the vector store.

    Returns:
        str: Vector Store ID.

    Raises:
        RuntimeError:
            If the SDK does not support Vector Stores at all.
    """
    # Prefer beta surface (most modern)
    if _has_beta_vs(client):
        try:
            vs = client.beta.vector_stores.create(name=name)
            return vs.id
        except Exception:
            # Fall through to non-beta surface below
            pass

    # Use legacy non-beta surface if present
    if _has_vs(client):
        vs = client.vector_stores.create(name=name)
        return vs.id

    # No vector store support at all
    raise RuntimeError(
        "Vector Stores are not available in this openai package. "
        "Upgrade with: pip install --upgrade openai"
    )


# ==============================================================================
# Upload File to Vector Store
# ==============================================================================


def upload_file_to_vs(
    client: OpenAI, vector_store_id: str, data: BytesIO, filename: str
) -> Dict[str, Any]:
    """
    Upload a file object to a given vector store ID.

    Attempts the most capable API path first:
        1. beta.vector_stores.file_batches.upload_and_poll
        2. beta.vector_stores.files.create (legacy attachment path)
        3. vector_stores.files.create (non-beta)
        4. Plain file upload without VS attachment (fallback)

    Parameters:
        client (OpenAI): Authenticated OpenAI SDK client.
        vector_store_id (str): The VS to attach file(s) to.
        data (BytesIO): Binary content of a file.
        filename (str): Original file name.

    Returns:
        Dict[str, Any]: A small structured status dictionary:
            {
                "status": "<completed|error|...>",
                "via": "beta.file_batches" | "beta.files.create+attach" | ...
                "file_id": "...",     # only in fallback
                "hint": "...",        # only in fallback (no VS support)
            }
    """
    if not vector_store_id:
        raise ValueError("vector_store_id is required")

    # ----- Path 1: Modern SDK — batch upload (preferred) ----------------------
    if _has_beta_vs(client):
        try:
            data.seek(0)
            data = _name_stream(data, filename)
            batch = client.beta.vector_stores.file_batches.upload_and_poll(
                vector_store_id=vector_store_id,
                files=[data],
            )
            return {
                "status": getattr(batch, "status", "completed"),
                "via": "beta.file_batches",
            }
        except AttributeError:
            # Some mid-range SDKs lack file_batches entirely → proceed below
            pass
        except Exception as e:
            # Batch path failed unexpectedly; continue to fallback
            return {"status": "error", "error": str(e), "via": "beta.file_batches"}

        # ----- Path 2: Beta interface + older attachment model ---------------
        try:
            f = _upload_file_object(client, data, filename)
            client.beta.vector_stores.files.create(
                vector_store_id=vector_store_id,
                file_id=f.id,
            )
            return {"status": "completed", "via": "beta.files.create+attach"}
        except Exception as e:
            # This is an error inside beta fallback
            return {"status": "error", "error": str(e), "via": "beta.fallback"}

    # ----- Path 3: Non-beta vector stores -----------------------------------
    if _has_vs(client):
        f = _upload_file_object(client, data, filename)
        client.vector_stores.files.create(vector_store_id=vector_store_id, file_id=f.id)
        return {"status": "completed", "via": "vector_stores.files.create"}

    # ----- Path 4: No VS support — Upload file only --------------------------
    f = _upload_file_object(client, data, filename)
    return {
        "status": "uploaded_file_only_no_vector_store_support",
        "file_id": f.id,
        "via": "files.create",
        "hint": (
            "This OpenAI package does not support Vector Stores. "
            "Upgrade with: pip install --upgrade openai"
        ),
    }

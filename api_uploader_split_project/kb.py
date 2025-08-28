# kb.py
from io import BytesIO
from typing import Dict, Any
from openai import OpenAI


def ensure_client(api_key: str) -> OpenAI:
    if not api_key:
        raise ValueError("OpenAI API key is required")
    return OpenAI(api_key=api_key)


# ---------- capability checks ----------
def _has_beta_vs(client: OpenAI) -> bool:
    return hasattr(client, "beta") and hasattr(client.beta, "vector_stores")


def _has_vs(client: OpenAI) -> bool:
    return hasattr(client, "vector_stores")


def vector_store_supported(client: OpenAI) -> bool:
    """Return True if either the beta or non-beta Vector Store surface exists."""
    return _has_beta_vs(client) or _has_vs(client)


# ---------- helpers ----------
def _name_stream(stream: BytesIO, filename: str) -> BytesIO:
    try:
        if hasattr(stream, "read") and not getattr(stream, "name", None):
            stream.name = filename
    except Exception:
        pass
    return stream


def _upload_file_object(client: OpenAI, data: BytesIO, filename: str):
    """
    Support both signatures seen in the wild:
      - files.create(file=(filename, file_like), purpose="assistants")
      - files.create(file=file_like, purpose="assistants", filename="...")
    """
    data.seek(0)
    data = _name_stream(data, filename)
    try:
        return client.files.create(file=(filename, data), purpose="assistants")
    except TypeError:
        return client.files.create(file=data, purpose="assistants", filename=filename)


# ---------- public API used by app.py ----------
def create_vector_store(client: OpenAI, name: str = "Canvas Templates / KB") -> str:
    """
    Create a vector store on whatever surface your SDK exposes.
    """
    # Prefer beta when present
    if _has_beta_vs(client):
        try:
            vs = client.beta.vector_stores.create(name=name)
            return vs.id
        except Exception:
            # fall through and try non-beta below
            pass

    if _has_vs(client):
        vs = client.vector_stores.create(name=name)
        return vs.id

    raise RuntimeError(
        "Vector Stores are not available in this openai package. "
        "Upgrade with: pip install --upgrade openai"
    )


def upload_file_to_vs(client: OpenAI, vector_store_id: str, data: BytesIO, filename: str) -> Dict[str, Any]:
    """
    Upload a file to an existing vector store. Tries the most capable path first.
    Returns a small status dict.
    """
    if not vector_store_id:
        raise ValueError("vector_store_id is required")

    # Newer SDKs: batch upload (beta)
    if _has_beta_vs(client):
        # try file_batches first
        try:
            data.seek(0)
            data = _name_stream(data, filename)
            batch = client.beta.vector_stores.file_batches.upload_and_poll(
                vector_store_id=vector_store_id,
                files=[data],
            )
            return {"status": getattr(batch, "status", "completed"), "via": "beta.file_batches"}
        except AttributeError:
            # older mid-1.x: create file and attach via beta.files.create
            try:
                f = _upload_file_object(client, data, filename)
                client.beta.vector_stores.files.create(
                    vector_store_id=vector_store_id,
                    file_id=f.id,
                )
                return {"status": "completed", "via": "beta.files.create+attach"}
            except Exception as e:
                return {"status": "error", "error": str(e), "via": "beta.fallback"}

    # Older SDKs: non-beta vector_stores
    if _has_vs(client):
        f = _upload_file_object(client, data, filename)
        client.vector_stores.files.create(vector_store_id=vector_store_id, file_id=f.id)
        return {"status": "completed", "via": "vector_stores.files.create"}

    # No VS support at all — still upload file (so user at least has it in Files)
    f = _upload_file_object(client, data, filename)
    return {
        "status": "uploaded_file_only_no_vector_store_support",
        "file_id": f.id,
        "via": "files.create",
        "hint": "Upgrade openai to enable Vector Stores: pip install --upgrade openai",
    }

# ------------------------------------------------------------------------------
# Refactor date: 2025-11-16
# Refactored by: Imaad Fakier
#
# Purpose:
#   End-to-end RAG pipeline foundation using:
#       • MongoDB Atlas Vector Search
#       • LangChain 0.3+ LCEL architecture
#       • OpenAI embeddings + ChatOpenAI
#
# Why this module exists:
#   - Centralises PDF ingestion, metadata hashing, chunking, and embedding flow.
#   - Provides a safe “check before embed” behaviour using SHA-256 fingerprinting.
#   - Creates a minimal + reliable retrieval chain for RAG micro-apps.
#   - Ensures micro-apps do not duplicate embedding logic.
#
# Notes for maintainers:
#   • This module assumes LangChain 0.3+ generation.
#   • Embeddings use OpenAIEmbeddings → required for Atlas Vector Search.
#   • Retrieval uses standard LCEL `{ context, question }` pattern.
#   • This module does NOT define the RAG handler (that lives in handlers.py).
#
# Future-proofing:
#   ✔ Single place to upgrade embeddings model
#   ✔ Ready for multimodal future ingestion (OCR pipeline can slot in)
#   ✔ Supports streaming LLM outputs if needed (LCEL ready)
# ------------------------------------------------------------------------------

import os
import uuid
import hashlib
from dotenv import load_dotenv

from pymongo import MongoClient

# LangChain (Modern 0.3+)
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_mongodb import MongoDBAtlasVectorSearch
from langchain_community.document_loaders import PyPDFLoader  # type: ignore
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser
from langchain_community.callbacks import get_openai_callback  # type: ignore

# ----------------------------------------------------------------------
# Environment loading
# ----------------------------------------------------------------------
load_dotenv()

mongo_uri = os.getenv("MONGO_DB_URI")
db_name = os.getenv("DATABASE_NAME")
files_metadata_collection_name = os.getenv("META_COLLECTION")
embeddings_collection_name = os.getenv("EMBEDDINGS_COLLECTION")
openai_api_key = os.getenv("OPENAI_API_KEY")

if not mongo_uri:
    raise RuntimeError("Missing MONGO_DB_URI environment variable.")
if not db_name:
    raise RuntimeError("Missing DATABASE_NAME environment variable.")
if not files_metadata_collection_name:
    raise RuntimeError("Missing META_COLLECTION environment variable.")
if not embeddings_collection_name:
    raise RuntimeError("Missing EMBEDDINGS_COLLECTION environment variable.")
if not openai_api_key:
    raise RuntimeError("Missing OPENAI_API_KEY environment variable.")

# ----------------------------------------------------------------------
# MongoDB initialisation
# ----------------------------------------------------------------------
client = MongoClient(mongo_uri)
db = client[db_name]

files_metadata = db[files_metadata_collection_name]
embeddings_collection = db[embeddings_collection_name]

ATLAS_VECTOR_SEARCH_INDEX_NAME = "vector_index"

# ----------------------------------------------------------------------
# Embeddings model (OpenAI)
#   • Uses LC v0.3+ standard OpenAIEmbeddings wrapper
# ----------------------------------------------------------------------
embeddings_model = OpenAIEmbeddings(openai_api_key=openai_api_key)


# ==============================================================================
#  SECTION 1 — Metadata Hashing & File Tracking
# ==============================================================================


def get_file_hash(file_path: str) -> str:
    """
    Compute SHA-256 digest of a file.

    Used to detect if a PDF has already been embedded.
    """
    hash_func = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            hash_func.update(chunk)
    return hash_func.hexdigest()


def check_and_store_metadata_and_embeddings(file_path: str) -> str:
    """
    Main ingestion entrypoint.

    1. Hash the file.
    2. If hash does not exist in metadata:
         • Load & chunk PDF
         • Generate embeddings
         • Store chunks + embeddings in MongoDB
    3. If metadata exists, skip embedding stage.

    Returns:
        str: Status message describing the action taken.
    """
    file_hash = get_file_hash(file_path)

    # Metadata check
    metadata = files_metadata.find_one({"filehash": file_hash})
    if metadata is not None:
        return "File metadata already exists."

    # Store metadata entry
    files_metadata.insert_one(
        {
            "_id": uuid.uuid4().hex,
            "filename": os.path.basename(file_path),
            "filehash": file_hash,
        }
    )
    print(f"Stored metadata for {os.path.basename(file_path)}")

    # Load PDF
    loader = PyPDFLoader(file_path)
    documents = loader.load()
    print(f"Loaded {len(documents)} documents from PDF.")

    # Chunking
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=300,
        chunk_overlap=100,
    )
    chunks = splitter.split_documents(documents)

    # Store embeddings in Atlas Vector Search
    MongoDBAtlasVectorSearch.from_documents(
        documents=chunks,
        embedding=embeddings_model,
        collection=embeddings_collection,
        index_name=ATLAS_VECTOR_SEARCH_INDEX_NAME,
    )

    return "Embeddings created and stored successfully."


# ==============================================================================
#  SECTION 2 — Retrieval Formatting
# ==============================================================================


def format_docs(docs) -> str:
    """
    Merge retrieved documents into a single formatted string.
    """
    return "\n\n".join(doc.page_content for doc in docs)


# ==============================================================================
#  SECTION 3 — Retrieval + Generation Pipeline (RAG)
# ==============================================================================


def retrieve_and_generate_response(question: str, template_text: str):
    """
    Execute a full RAG cycle:

    1. Vector search in MongoDB Atlas (similarity search)
    2. Prepare LCEL pipeline:
         • { context, question }
         • PromptTemplate
         • ChatOpenAI
         • OutputParser
    3. Track OpenAI cost using callback manager.

    Returns:
        (response_text, total_cost_usd)
    """
    # Build retriever
    vector_search = MongoDBAtlasVectorSearch(
        collection=embeddings_collection,
        index_name=ATLAS_VECTOR_SEARCH_INDEX_NAME,
        embedding=embeddings_model,
    )

    retriever = vector_search.as_retriever(
        search_type="similarity",
        search_kwargs={"k": 1},
    )

    # Prompt
    prompt = PromptTemplate.from_template(template_text)

    # Model
    model = ChatOpenAI(
        api_key=openai_api_key,
        model_name="gpt-4o",
        temperature=0.7,
    )

    # LCEL chain
    chain = (
        {
            "context": retriever | format_docs,
            "question": RunnablePassthrough(),
        }
        | prompt
        | model
        | StrOutputParser()
    )

    # Cost tracking
    with get_openai_callback() as cb:
        response = chain.invoke(question)
        cost = cb.total_cost

    return response, cost

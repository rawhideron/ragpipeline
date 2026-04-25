import hashlib
import io
import os
import re
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel
from pypdf import PdfReader
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, PointStruct, VectorParams

try:
    from anthropic import Anthropic
except Exception:  # pragma: no cover
    Anthropic = None


QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
COLLECTION = os.getenv("QDRANT_COLLECTION", "rag_documents")
GENERATION_PROVIDER = os.getenv("GENERATION_PROVIDER", "openai").lower()
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", GENERATION_PROVIDER).lower()
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-5-mini")
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
OLLAMA_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "qwen2.5:1.5b")
OLLAMA_EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
VECTOR_SIZE = int(os.getenv("VECTOR_SIZE", "1536"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "8000"))

app = FastAPI(title="RAG Pipeline API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

qdrant = QdrantClient(url=QDRANT_URL)
openai_client: OpenAI | None = None
http_client = httpx.Client(timeout=120)


class QueryRequest(BaseModel):
    question: str
    top_k: int = 5


def ensure_collection() -> None:
    collections = qdrant.get_collections().collections
    if any(collection.name == COLLECTION for collection in collections):
        return
    qdrant.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )


def get_openai_client() -> OpenAI:
    global openai_client
    if openai_client is None:
        openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return openai_client


def extract_text(filename: str, payload: bytes) -> str:
    if filename.lower().endswith(".pdf"):
        reader = PdfReader(io.BytesIO(payload))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    return payload.decode("utf-8", errors="ignore")


def chunk_text(text: str, max_chars: int = 1400, overlap: int = 200) -> list[str]:
    clean = re.sub(r"\s+", " ", text).strip()
    chunks: list[str] = []
    start = 0
    while start < len(clean):
        end = min(start + max_chars, len(clean))
        chunks.append(clean[start:end])
        if end == len(clean):
            break
        start = max(0, end - overlap)
    return [chunk for chunk in chunks if chunk.strip()]


def embed_with_openai(texts: list[str]) -> list[list[float]]:
    response = get_openai_client().embeddings.create(model=OPENAI_EMBEDDING_MODEL, input=texts)
    return [item.embedding for item in response.data]


def embed_with_ollama(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for text in texts:
        response = http_client.post(
            f"{OLLAMA_BASE_URL}/api/embeddings",
            json={"model": OLLAMA_EMBEDDING_MODEL, "prompt": text},
        )
        response.raise_for_status()
        vectors.append(response.json()["embedding"])
    return vectors


def embed_texts(texts: list[str]) -> list[list[float]]:
    if EMBEDDING_PROVIDER == "ollama":
        return embed_with_ollama(texts)
    return embed_with_openai(texts)


def document_id(filename: str, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()[:16]
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", filename).strip("-")
    return f"{safe_name}-{digest}"


def answer_with_openai(question: str, context: str) -> str:
    response = get_openai_client().responses.create(
        model=OPENAI_CHAT_MODEL,
        input=[
            {
                "role": "system",
                "content": (
                    "Answer using only the supplied context. If the context does "
                    "not contain the answer, say you do not know from the uploaded documents."
                ),
            },
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"},
        ],
    )
    return response.output_text


def answer_with_anthropic(question: str, context: str) -> str:
    if Anthropic is None:
        raise HTTPException(status_code=500, detail="Anthropic SDK is not installed")
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=800,
        system=(
            "Answer using only the supplied context. If the context does not contain "
            "the answer, say you do not know from the uploaded documents."
        ),
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}],
    )
    return "\n".join(block.text for block in message.content if getattr(block, "type", "") == "text")


def answer_with_ollama(question: str, context: str) -> str:
    response = http_client.post(
        f"{OLLAMA_BASE_URL}/api/chat",
        json={
            "model": OLLAMA_CHAT_MODEL,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Answer using only the supplied context. If the context does not contain "
                        "the answer, say you do not know from the uploaded documents."
                    ),
                },
                {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"},
            ],
        },
    )
    response.raise_for_status()
    return response.json()["message"]["content"]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ingest")
async def ingest(file: UploadFile = File(...), source: str = Form(default="upload")) -> dict[str, Any]:
    payload = await file.read()
    text = extract_text(file.filename or "document", payload)
    chunks = chunk_text(text)
    if not chunks:
        raise HTTPException(status_code=400, detail="No text could be extracted from this document")

    ensure_collection()
    doc_id = document_id(file.filename or "document", payload)
    vectors = embed_texts(chunks)
    points = [
        PointStruct(
            id=str(uuid4()),
            vector=vector,
            payload={
                "document_id": doc_id,
                "filename": file.filename,
                "source": source,
                "chunk_index": index,
                "text": chunk,
            },
        )
        for index, (chunk, vector) in enumerate(zip(chunks, vectors))
    ]
    qdrant.upsert(collection_name=COLLECTION, points=points)
    return {"document_id": doc_id, "chunks": len(points), "filename": file.filename}


@app.post("/query")
def query(request: QueryRequest) -> dict[str, Any]:
    ensure_collection()
    query_vector = embed_texts([request.question])[0]
    hits = qdrant.search(
        collection_name=COLLECTION,
        query_vector=query_vector,
        limit=max(1, min(request.top_k, 12)),
        with_payload=True,
    )
    sources = [
        {
            "score": hit.score,
            "filename": hit.payload.get("filename"),
            "document_id": hit.payload.get("document_id"),
            "chunk_index": hit.payload.get("chunk_index"),
            "text": hit.payload.get("text"),
        }
        for hit in hits
    ]
    context = "\n\n".join(source["text"] or "" for source in sources)[:MAX_CONTEXT_CHARS]
    if GENERATION_PROVIDER == "anthropic":
        answer = answer_with_anthropic(request.question, context)
    elif GENERATION_PROVIDER == "ollama":
        answer = answer_with_ollama(request.question, context)
    else:
        answer = answer_with_openai(request.question, context)
    return {"answer": answer, "sources": sources}

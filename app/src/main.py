import hashlib
import ipaddress
import io
import os
import re
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from uuid import NAMESPACE_URL, uuid5

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel
from pypdf import PdfReader
from qdrant_client import QdrantClient
from qdrant_client.http.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointStruct,
    VectorParams,
)

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
INGEST_USER_AGENT = os.getenv("INGEST_USER_AGENT", "ragpipeline/0.1")
MAX_URL_BYTES = int(os.getenv("MAX_URL_BYTES", str(5 * 1024 * 1024)))
ALLOW_PRIVATE_URL_INGEST = os.getenv("ALLOW_PRIVATE_URL_INGEST", "false").lower() == "true"

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


class UrlIngestRequest(BaseModel):
    url: str
    replace: bool = True


class ExtractedHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title = " ".join([self.title, text]).strip()
        elif not self._skip_depth:
            self._parts.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self._parts)


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


def search_points(query_vector: list[float], limit: int) -> list[Any]:
    if hasattr(qdrant, "query_points"):
        return qdrant.query_points(
            collection_name=COLLECTION,
            query=query_vector,
            limit=limit,
            with_payload=True,
        ).points
    return qdrant.search(
        collection_name=COLLECTION,
        query_vector=query_vector,
        limit=limit,
        with_payload=True,
    )


def content_hash(content: bytes | str) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def safe_label(value: str, fallback: str = "document") -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")
    return safe[:72] or fallback


def document_id(source_type: str, source_uri: str) -> str:
    digest = hashlib.sha256(f"{source_type}:{source_uri}".encode("utf-8")).hexdigest()[:16]
    return f"{safe_label(source_uri)}-{digest}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def document_filter(doc_id: str) -> Filter:
    return Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=doc_id))])


def delete_document_points(doc_id: str) -> None:
    ensure_collection()
    qdrant.delete(collection_name=COLLECTION, points_selector=FilterSelector(filter=document_filter(doc_id)))


def upsert_document_chunks(
    *,
    doc_id: str,
    source_type: str,
    source_uri: str,
    title: str | None,
    filename: str | None,
    source: str,
    text: str,
    license_name: str | None = None,
    attribution: str | None = None,
    replace: bool = True,
) -> dict[str, Any]:
    chunks = chunk_text(text)
    if not chunks:
        raise HTTPException(status_code=400, detail="No text could be extracted from this document")

    if replace:
        delete_document_points(doc_id)
    else:
        ensure_collection()

    created_at = utc_now()
    hash_value = content_hash(text)
    version_id = hash_value[:16]
    vectors = embed_texts(chunks)
    points = [
        PointStruct(
            id=str(uuid5(NAMESPACE_URL, f"{doc_id}:{version_id}:{index}")),
            vector=vector,
            payload={
                "document_id": doc_id,
                "source_type": source_type,
                "source_uri": source_uri,
                "filename": filename,
                "title": title,
                "source": source,
                "chunk_index": index,
                "chunk_count": len(chunks),
                "content_hash": hash_value,
                "version_id": version_id,
                "ingested_at": created_at,
                "updated_at": created_at,
                "license": license_name,
                "attribution": attribution,
                "text": chunk,
            },
        )
        for index, (chunk, vector) in enumerate(zip(chunks, vectors))
    ]
    qdrant.upsert(collection_name=COLLECTION, points=points)
    return {
        "document_id": doc_id,
        "chunks": len(points),
        "source_type": source_type,
        "source_uri": source_uri,
        "title": title,
        "filename": filename,
        "content_hash": hash_value,
        "version_id": version_id,
    }


def validate_http_url(raw_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise HTTPException(status_code=400, detail="URL must be an absolute http or https URL")
    if not ALLOW_PRIVATE_URL_INGEST:
        try:
            addresses = socket.getaddrinfo(parsed.hostname, None)
        except socket.gaierror as exc:
            raise HTTPException(status_code=400, detail="Could not resolve URL hostname") from exc
        for *_, sockaddr in addresses:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise HTTPException(status_code=400, detail="Private URL ingestion is disabled")
    return raw_url.strip()


def assert_robots_allowed(url: str) -> None:
    parsed = urlparse(url)
    robots_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/robots.txt")
    try:
        response = http_client.get(robots_url, headers={"User-Agent": INGEST_USER_AGENT})
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=f"Could not verify robots.txt for {parsed.netloc}") from exc

    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(response.text.splitlines())
    if not parser.can_fetch(INGEST_USER_AGENT, url):
        raise HTTPException(status_code=403, detail="robots.txt does not allow ingestion of this URL")


def fetch_url_text(url: str) -> tuple[str, str | None]:
    assert_robots_allowed(url)
    try:
        with http_client.stream("GET", url, headers={"User-Agent": INGEST_USER_AGENT}, follow_redirects=True) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                raise HTTPException(status_code=400, detail=f"Unsupported URL content type: {content_type}")

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_URL_BYTES:
                    raise HTTPException(status_code=413, detail="URL content is too large to ingest")
                chunks.append(chunk)
            payload = b"".join(chunks)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=400, detail=f"URL fetch failed with status {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail="URL fetch failed") from exc

    text = payload.decode(response.encoding or "utf-8", errors="ignore")
    if "text/html" not in content_type:
        return text, None

    parser = ExtractedHtml()
    parser.feed(text)
    return parser.text, parser.title or None


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
    filename = file.filename or "document"
    text = extract_text(filename, payload)
    return upsert_document_chunks(
        doc_id=document_id("upload", filename),
        source_type="upload",
        source_uri=filename,
        title=filename,
        filename=filename,
        source=source,
        text=text,
        replace=True,
    )


@app.post("/ingest/url")
def ingest_url(request: UrlIngestRequest) -> dict[str, Any]:
    url = validate_http_url(request.url)
    text, title = fetch_url_text(url)
    return upsert_document_chunks(
        doc_id=document_id("url", url),
        source_type="url",
        source_uri=url,
        title=title or url,
        filename=None,
        source=url,
        text=text,
        attribution=url,
        replace=request.replace,
    )


@app.get("/documents")
def list_documents() -> dict[str, Any]:
    ensure_collection()
    documents: dict[str, dict[str, Any]] = {}
    offset = None
    while True:
        points, offset = qdrant.scroll(
            collection_name=COLLECTION,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            doc_id = payload.get("document_id")
            if not doc_id:
                continue
            current = documents.setdefault(
                doc_id,
                {
                    "document_id": doc_id,
                    "source_type": payload.get("source_type") or "upload",
                    "source_uri": payload.get("source_uri") or payload.get("filename"),
                    "title": payload.get("title") or payload.get("filename") or doc_id,
                    "filename": payload.get("filename"),
                    "source": payload.get("source"),
                    "content_hash": payload.get("content_hash"),
                    "version_id": payload.get("version_id"),
                    "ingested_at": payload.get("ingested_at"),
                    "updated_at": payload.get("updated_at"),
                    "license": payload.get("license"),
                    "attribution": payload.get("attribution"),
                    "chunks": 0,
                },
            )
            current["chunks"] += 1
            for key in ("ingested_at", "updated_at"):
                value = payload.get(key)
                if value and (not current.get(key) or value > current[key]):
                    current[key] = value
        if offset is None:
            break
    return {"documents": sorted(documents.values(), key=lambda item: item.get("updated_at") or "", reverse=True)}


@app.delete("/documents/{doc_id}")
def delete_document(doc_id: str) -> dict[str, Any]:
    delete_document_points(doc_id)
    return {"document_id": doc_id, "deleted": True}


@app.post("/query")
def query(request: QueryRequest) -> dict[str, Any]:
    ensure_collection()
    query_vector = embed_texts([request.question])[0]
    hits = search_points(query_vector, max(1, min(request.top_k, 12)))
    sources = [
        {
            "score": hit.score,
            "filename": hit.payload.get("filename"),
            "document_id": hit.payload.get("document_id"),
            "source_type": hit.payload.get("source_type"),
            "source_uri": hit.payload.get("source_uri"),
            "title": hit.payload.get("title"),
            "chunk_index": hit.payload.get("chunk_index"),
            "license": hit.payload.get("license"),
            "attribution": hit.payload.get("attribution"),
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

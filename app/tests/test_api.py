from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import main


class FakeQdrant:
    def __init__(self, pages):
        self.pages = pages
        self.offsets = []

    def scroll(self, *, offset, **_kwargs):
        self.offsets.append(offset)
        index = offset or 0
        next_offset = index + 1 if index + 1 < len(self.pages) else None
        return [SimpleNamespace(payload=payload) for payload in self.pages[index]], next_offset


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "ensure_collection", lambda: None)
    return TestClient(main.app)


def test_list_documents_groups_chunks_across_pages(monkeypatch, client):
    fake = FakeQdrant(
        [
            [
                {"document_id": "a", "filename": "a.pdf", "ingested_at": "2026-01-01", "updated_at": "2026-01-02"},
                {"text": "chunk without a document id"},
                {"document_id": "b", "title": "B", "source_type": "url", "updated_at": "2026-03-01"},
            ],
            [
                {"document_id": "a", "filename": "a.pdf", "ingested_at": "2026-01-01", "updated_at": "2026-02-01"},
                None,
            ],
        ]
    )
    monkeypatch.setattr(main, "qdrant", fake)

    documents = client.get("/documents").json()["documents"]

    assert fake.offsets == [None, 1]
    assert [doc["document_id"] for doc in documents] == ["b", "a"]
    doc_b, doc_a = documents
    assert doc_a["chunks"] == 2
    assert doc_a["title"] == "a.pdf"
    assert doc_a["source_uri"] == "a.pdf"
    assert doc_a["source_type"] == "upload"
    assert doc_a["ingested_at"] == "2026-01-01"
    assert doc_a["updated_at"] == "2026-02-01"
    assert doc_b["chunks"] == 1
    assert doc_b["title"] == "B"
    assert doc_b["source_type"] == "url"


def test_ingest_reads_file_and_source_form_fields(monkeypatch, client):
    calls = []
    monkeypatch.setattr(main, "extract_text", lambda filename, payload: payload.decode())
    monkeypatch.setattr(main, "upsert_document_chunks", lambda **kwargs: calls.append(kwargs) or {"ok": True})

    default = client.post("/ingest", files={"file": ("notes.txt", b"hello", "text/plain")})
    custom = client.post("/ingest", files={"file": ("notes.txt", b"hello", "text/plain")}, data={"source": "email"})

    assert default.status_code == custom.status_code == 200
    assert [call["source"] for call in calls] == ["upload", "email"]
    assert calls[0]["text"] == "hello"
    assert calls[0]["filename"] == "notes.txt"


def test_ingest_requires_a_file(client):
    assert client.post("/ingest", data={"source": "email"}).status_code == 422


def test_get_ingest_job_returns_404_for_unknown_job(client):
    response = client.get("/ingest/jobs/missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "Ingest job not found"


@pytest.mark.parametrize(
    ("method", "path", "codes"),
    [
        ("post", "/ingest", {"400"}),
        ("post", "/ingest/url", {"400", "403", "413"}),
        ("get", "/ingest/jobs/{job_id}", {"404"}),
        ("post", "/query", {"500"}),
        ("patch", "/documents/{doc_id}", {"400", "404"}),
    ],
)
def test_openapi_documents_error_responses(method, path, codes):
    documented = set(main.app.openapi()["paths"][path][method]["responses"])

    assert codes <= documented


def make_hit(score, text="Kubernetes scales containers.", **payload):
    return SimpleNamespace(
        score=score,
        payload={"title": "Kubernetes", "source_uri": "https://kubernetes.io/", "text": text, **payload},
    )


def test_query_refuses_without_calling_the_model_when_nothing_is_relevant(monkeypatch, client):
    monkeypatch.setattr(main, "embed_texts", lambda texts, kind="document": [[0.0]])
    monkeypatch.setattr(main, "search_points", lambda vector, limit: [make_hit(0.5)])
    monkeypatch.setattr(main, "answer_with_ollama", lambda q, c: pytest.fail("model should not be called"))
    monkeypatch.setattr(main, "GENERATION_PROVIDER", "ollama")

    response = client.post("/query", json={"question": "Who won the 1998 World Cup?"})

    assert response.json() == {"answer": main.NO_ANSWER, "sources": []}


def test_query_sends_numbered_relevant_sources_and_keeps_cited_answer(monkeypatch, client):
    seen = {}
    monkeypatch.setattr(main, "embed_texts", lambda texts, kind="document": seen.setdefault("kind", kind) and [[0.0]])
    monkeypatch.setattr(main, "search_points", lambda vector, limit: [make_hit(0.8), make_hit(0.4, text="noise")])
    monkeypatch.setattr(main, "answer_with_ollama", lambda q, c: seen.setdefault("context", c) and "It scales. [1]")
    monkeypatch.setattr(main, "supported_sentences_with_ollama", lambda c, sentences: [1])
    monkeypatch.setattr(main, "GENERATION_PROVIDER", "ollama")

    body = client.post("/query", json={"question": "How does Kubernetes scale?"}).json()

    assert seen["kind"] == "query"
    assert seen["context"] == "[1] Kubernetes (https://kubernetes.io/)\nKubernetes scales containers."
    assert body["answer"] == "It scales. [1]"
    assert len(body["sources"]) == 1


def test_query_replaces_an_answer_that_cites_no_source(monkeypatch, client):
    monkeypatch.setattr(main, "embed_texts", lambda texts, kind="document": [[0.0]])
    monkeypatch.setattr(main, "search_points", lambda vector, limit: [make_hit(0.8)])
    monkeypatch.setattr(main, "answer_with_ollama", lambda q, c: "Kubernetes was written in 1850. [7]")
    monkeypatch.setattr(main, "supported_sentences_with_ollama", lambda c, sentences: [1])
    monkeypatch.setattr(main, "GENERATION_PROVIDER", "ollama")

    body = client.post("/query", json={"question": "When was Kubernetes written?"}).json()

    assert body["answer"] == main.NO_ANSWER


def test_prepare_for_nomic_lowercases_and_adds_task_prefixes():
    assert main.prepare_for_nomic(["What is Kubernetes?"], "query") == ["search_query: what is kubernetes?"]
    assert main.prepare_for_nomic(["Kubernetes Docs"], "document") == ["search_document: kubernetes docs"]


def test_reembed_collection_updates_vectors_from_payload_text(monkeypatch):
    class FakeQdrant:
        def __init__(self):
            self.updated = []

        def scroll(self, *, offset, **_kwargs):
            pages = [
                [SimpleNamespace(id="a", payload={"text": "One"}), SimpleNamespace(id="b", payload={})],
                [SimpleNamespace(id="c", payload={"text": "Two"})],
            ]
            index = offset or 0
            return pages[index], (index + 1 if index == 0 else None)

        def update_vectors(self, *, points, **_kwargs):
            self.updated.extend(points)

    fake = FakeQdrant()
    monkeypatch.setattr(main, "qdrant", fake)
    monkeypatch.setattr(main, "embed_texts", lambda texts, kind: [[float(len(t))] for t in texts])

    assert main.reembed_collection() == 2
    assert [(p.id, p.vector) for p in fake.updated] == [("a", [3.0]), ("c", [3.0])]


def test_query_drops_sentences_the_checker_does_not_confirm(monkeypatch, client):
    monkeypatch.setattr(main, "embed_texts", lambda texts, kind="document": [[0.0]])
    monkeypatch.setattr(main, "search_points", lambda vector, limit: [make_hit(0.8)])
    monkeypatch.setattr(
        main, "answer_with_ollama", lambda q, c: "Kubernetes scales containers [1]. Pods are the smallest unit [1]."
    )
    monkeypatch.setattr(main, "supported_sentences_with_ollama", lambda c, sentences: [1, 99])
    monkeypatch.setattr(main, "GENERATION_PROVIDER", "ollama")

    body = client.post("/query", json={"question": "What is a pod?"}).json()

    assert body["answer"] == "Kubernetes scales containers [1]."


def test_query_refuses_when_the_checker_confirms_nothing(monkeypatch, client):
    monkeypatch.setattr(main, "embed_texts", lambda texts, kind="document": [[0.0]])
    monkeypatch.setattr(main, "search_points", lambda vector, limit: [make_hit(0.8)])
    monkeypatch.setattr(main, "answer_with_ollama", lambda q, c: "Pods are the smallest unit [1].")
    monkeypatch.setattr(main, "supported_sentences_with_ollama", lambda c, sentences: [])
    monkeypatch.setattr(main, "GENERATION_PROVIDER", "ollama")

    body = client.post("/query", json={"question": "What is a pod?"}).json()

    assert body["answer"] == main.NO_ANSWER


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        ("Kubernetes gives Pods their own IP addresses and a single DNS name", True),
        ("It gives Pods their own IP addresses and a single DNS name", True),
        ("Pods are the smallest deployable units in Kubernetes", False),
        ("Pods", False),
    ],
)
def test_quote_in_sources(quote, expected):
    context = "[1] Kubernetes\nKubernetes gives Pods their own IP addresses and a single DNS name for a set of Pods."
    assert main.quote_in_sources(quote, context) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("One. Two!", ["One.", "Two!"]),
        ("It scales. [1] Next one? [2][3] Done", ["It scales. [1]", "Next one? [2][3]", "Done"]),
        ("No ending", ["No ending"]),
        ("a.b c.", ["a.b c."]),
        ("Line one.\nLine two.\n\nThird", ["Line one.", "Line two.", "Third"]),
    ],
)
def test_split_sentences(text, expected):
    assert main.split_sentences(text) == expected


@pytest.fixture
def local_qdrant(monkeypatch):
    from qdrant_client import QdrantClient
    from qdrant_client.http.models import Distance, PointStruct, VectorParams

    store = QdrantClient(":memory:")
    store.create_collection(main.COLLECTION, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    base = {"title": "Old", "collection_id": None, "updated_at": "2026-01-01", "text": "chunk"}
    store.upsert(
        main.COLLECTION,
        points=[
            PointStruct(id=1, vector=[1.0, 0.0], payload={**base, "document_id": "doc-a", "chunk_index": 0}),
            PointStruct(id=2, vector=[0.0, 1.0], payload={**base, "document_id": "doc-a", "chunk_index": 1}),
            PointStruct(id=3, vector=[1.0, 1.0], payload={**base, "document_id": "doc-b", "chunk_index": 0}),
        ],
    )
    monkeypatch.setattr(main, "qdrant", store)
    return store


def test_patch_document_updates_payload_on_every_chunk_without_touching_vectors(local_qdrant, client):
    response = client.patch("/documents/doc-a", json={"title": "New title", "collection_id": "k8s", "license": None})

    assert response.status_code == 200
    body = response.json()
    assert body["chunks"] == 2
    assert body["updated"]["title"] == "New title"
    assert body["updated"]["updated_at"] != "2026-01-01"
    points = {p.id: p for p in local_qdrant.retrieve(main.COLLECTION, ids=[1, 2, 3], with_vectors=True)}
    for point_id in (1, 2):
        payload = points[point_id].payload
        assert payload["title"] == "New title"
        assert payload["collection_id"] == "k8s"
        assert payload.get("license") is None
        assert payload["text"] == "chunk"
        assert payload["chunk_index"] == point_id - 1
    assert points[1].vector == pytest.approx([1.0, 0.0])
    assert points[3].payload["title"] == "Old"
    assert points[3].payload["updated_at"] == "2026-01-01"


def test_patch_document_returns_404_for_unknown_document(local_qdrant, client):
    response = client.patch("/documents/missing", json={"title": "x"})

    assert response.status_code == 404


def test_patch_document_rejects_an_empty_update(local_qdrant, client):
    assert client.patch("/documents/doc-a", json={}).status_code == 400


@pytest.mark.parametrize("field", ["text", "document_id", "content_hash", "updated_at"])
def test_patch_document_rejects_fields_that_are_not_editable(local_qdrant, client, field):
    assert client.patch("/documents/doc-a", json={field: "x"}).status_code == 422

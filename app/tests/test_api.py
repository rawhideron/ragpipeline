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
    ],
)
def test_openapi_documents_error_responses(method, path, codes):
    documented = set(main.app.openapi()["paths"][path][method]["responses"])

    assert codes <= documented

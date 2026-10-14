import httpx
import pytest
from fastapi import HTTPException

from src import main

BASE_URL = "https://docs.python.org/3/"


def use_download_page(monkeypatch, html: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/3/download.html":
            return httpx.Response(200, text=html)
        return httpx.Response(404)

    monkeypatch.setattr(main, "http_client", httpx.Client(transport=httpx.MockTransport(handler)))


def test_returns_absolute_url_of_html_zip(monkeypatch):
    use_download_page(
        monkeypatch,
        '<a href="index.html">Home</a>'
        '<a href="archives/python-3.15-docs-pdf-a4.zip">PDF</a>'
        "<a href='archives/python-3.15-docs-html.zip'>HTML</a>"
        '<a href="archives/python-3.15-docs-text.zip">Text</a>',
    )

    assert main.discover_python_docs_html_zip(BASE_URL) == "https://docs.python.org/3/archives/python-3.15-docs-html.zip"


def test_ignores_html_zips_outside_python_archives(monkeypatch):
    use_download_page(
        monkeypatch,
        '<a href="other/python-3.15-docs-html.zip">Mirror</a>'
        '<a href="archives/python-3.15-docs-html.zip">HTML</a>',
    )

    assert main.discover_python_docs_html_zip(BASE_URL) == "https://docs.python.org/3/archives/python-3.15-docs-html.zip"


def test_raises_400_when_no_html_zip_link(monkeypatch):
    use_download_page(monkeypatch, '<a href="archives/python-3.15-docs-pdf-a4.zip">PDF</a>')

    with pytest.raises(HTTPException) as exc_info:
        main.discover_python_docs_html_zip(BASE_URL)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Could not find the Python docs HTML zip download link"

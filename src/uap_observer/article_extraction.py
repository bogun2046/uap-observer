"""Article text extraction queue and Trafilatura adapter."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import io
import json
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import ClassVar, Protocol
from urllib.parse import urlsplit

import urllib3

from uap_observer.repositories import Repository


@dataclass(frozen=True)
class ExtractedArticle:
    content: str
    title: str | None = None
    author: str | None = None
    publish_date: str | None = None
    language: str | None = None
    extractor: str = "unknown"


@dataclass(frozen=True)
class ExtractionRun:
    stale_recovered: int = 0
    queued: int = 0
    claimed: int = 0
    completed: int = 0
    failed: int = 0
    skipped_duplicates: int = 0


class ArticleExtractor(Protocol):
    def extract_url(self, url: str) -> ExtractedArticle:
        ...


class TrafilaturaArticleExtractor:
    """Download and extract a readable article using a pinned Trafilatura API."""

    _user_agent = "UAPObserver/0.1 (+public-source research)"

    def __init__(self, *, minimum_characters: int = 200) -> None:
        self.minimum_characters = minimum_characters

    @staticmethod
    def _module():
        try:
            return importlib.import_module("trafilatura")
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "Trafilatura is not installed; run 'python -m pip install -e .'"
            ) from error

    @staticmethod
    def _extractor_id() -> str:
        try:
            version = importlib.metadata.version("trafilatura")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        return f"trafilatura/{version}"

    def extract_url(self, url: str) -> ExtractedArticle:
        if _looks_like_pdf(url):
            return self.extract_pdf_url(url)
        trafilatura = self._module()
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            downloaded, content_type = self._fallback_download(url)
            if content_type == "application/pdf" or (
                isinstance(downloaded, bytes) and _is_pdf_payload(downloaded)
            ):
                return self.extract_pdf(downloaded, url=url)
        return self.extract_html(downloaded, url=url)

    def _fallback_download(self, url: str) -> tuple[bytes, str]:
        response = urllib3.PoolManager().request(
            "GET",
            url,
            headers={"User-Agent": self._user_agent, "Accept": "text/html,application/pdf"},
            timeout=30.0,
            preload_content=True,
        )
        if response.status >= 400 or not response.data:
            raise RuntimeError(f"Unable to download article ({response.status}): {url}")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        return response.data, content_type

    def extract_pdf_url(self, url: str) -> ExtractedArticle:
        """Download and extract text from an official PDF document."""

        response = urllib3.PoolManager().request(
            "GET", url, timeout=30.0, preload_content=True
        )
        if response.status >= 400:
            raise RuntimeError(f"Unable to download PDF ({response.status}): {url}")
        return self.extract_pdf(response.data, url=url)

    def extract_pdf(self, pdf: bytes, *, url: str) -> ExtractedArticle:
        try:
            from pypdf import PdfReader
        except ModuleNotFoundError as error:
            message = "pypdf is not installed; run 'python -m pip install -e .'"
            raise RuntimeError(message) from error
        reader = PdfReader(io.BytesIO(pdf))
        content = "\n\n".join(
            text.strip()
            for page in reader.pages
            if (text := page.extract_text() or "").strip()
        )
        if len(content) < self.minimum_characters:
            raise RuntimeError(
                f"Extracted PDF content is too short: {len(content)} characters"
            )
        metadata = reader.metadata
        return ExtractedArticle(
            content=content,
            title=getattr(metadata, "title", None) if metadata else None,
            author=getattr(metadata, "author", None) if metadata else None,
            extractor="pypdf",
        )

    def extract_html(self, html: str | bytes, *, url: str) -> ExtractedArticle:
        trafilatura = self._module()
        result = trafilatura.extract(
            html,
            url=url,
            output_format="json",
            with_metadata=True,
            include_comments=False,
            include_tables=False,
        )
        payload = json.loads(result) if result else {}
        content = _normalize_text(payload.get("text"))
        extractor = self._extractor_id()
        if len(content) < self.minimum_characters:
            fallback = _FallbackHtmlExtractor().extract(html)
            content = _normalize_text(fallback.content)
            if len(content) >= self.minimum_characters:
                extractor = fallback.extractor
                payload = {
                    **payload,
                    "title": payload.get("title") or fallback.title,
                    "author": payload.get("author") or fallback.author,
                    "date": payload.get("date") or fallback.publish_date,
                    "language": payload.get("language") or fallback.language,
                }
        if len(content) < self.minimum_characters:
            raise RuntimeError(f"Extracted content is too short: {len(content)} characters")
        return ExtractedArticle(
            content=content,
            title=payload.get("title"),
            author=payload.get("author"),
            publish_date=payload.get("date"),
            language=payload.get("language"),
            extractor=extractor,
        )


class _FallbackHtmlExtractor(HTMLParser):
    """Small dependency-free fallback for official pages with unusual markup."""

    _ignored: ClassVar[set[str]] = {
        "script",
        "style",
        "nav",
        "footer",
        "header",
        "svg",
        "noscript",
    }

    def __init__(self) -> None:
        super().__init__()
        self._ignored_depth = 0
        self._in_content = False
        self._content_depth = 0
        self._parts: list[str] = []
        self._title_parts: list[str] = []
        self._meta: dict[str, str] = {}
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in self._ignored:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True
        marker = f"{attributes.get('id', '')} {attributes.get('class', '')}".lower()
        is_content_container = tag in {"main", "article"} or any(
            token in marker for token in ("article", "content", "release", "body")
        )
        if is_content_container and self._content_depth == 0:
            self._in_content = True
            self._content_depth = 1
        elif self._in_content and tag not in self._ignored:
            self._content_depth += 1
        if tag == "meta":
            name = (attributes.get("name") or attributes.get("property") or "").lower()
            value = attributes.get("content")
            if name and value:
                self._meta[name] = value.strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self._ignored and self._ignored_depth:
            self._ignored_depth -= 1
        if self._in_content and tag not in self._ignored:
            self._content_depth -= 1
            if self._content_depth <= 0:
                self._in_content = False
                self._content_depth = 0

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text or self._ignored_depth:
            return
        if self._in_title:
            self._title_parts.append(text)
        if self._in_content:
            self._parts.append(text)

    def extract(self, html: str | bytes) -> ExtractedArticle:
        self.feed(html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html)
        content = "\n\n".join(self._parts)
        if not content:
            content = self._meta.get("description", "")
        return ExtractedArticle(
            content=content,
            title=" ".join(self._title_parts).strip() or None,
            author=self._meta.get("author"),
            publish_date=self._meta.get("article:published_time") or self._meta.get("date"),
            language=self._meta.get("language") or self._meta.get("og:locale"),
            extractor="html-fallback",
        )


def _looks_like_pdf(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(".pdf")


def _is_pdf_payload(payload: bytes) -> bool:
    return payload[:5] == b"%PDF-"


def _normalize_text(value: object) -> str:
    return "\n\n".join(
        line.strip()
        for line in str(value or "").splitlines()
        if line.strip()
    )


class ArticleExtractionService:
    def __init__(
        self,
        repository: Repository,
        extractor: ArticleExtractor | None = None,
    ) -> None:
        self.repository = repository
        self.extractor = extractor or TrafilaturaArticleExtractor()

    def run(self, *, limit: int, retry_failed: bool = False) -> ExtractionRun:
        stale_recovered = self.repository.reset_stale_article_tasks()
        tasks = self.repository.get_article_tasks(
            limit=limit,
            retry_failed=retry_failed,
        )
        claimed = completed = failed = skipped = 0
        for task in tasks:
            if not self.repository.claim_article_task(
                task.news_id,
                retry_failed=retry_failed,
            ):
                continue
            claimed += 1
            try:
                article = self.extractor.extract_url(task.url)
                content_hash = hashlib.sha256(article.content.encode("utf-8")).hexdigest()
                duplicate_id = self.repository.find_news_by_content_hash(
                    content_hash,
                    exclude_news_id=task.news_id,
                )
                if duplicate_id is not None:
                    self.repository.skip_duplicate_article(
                        task.news_id,
                        duplicate_of_news_id=duplicate_id,
                        content_hash=content_hash,
                        extracted_by=article.extractor,
                    )
                    skipped += 1
                    continue
                self.repository.complete_article_extraction(
                    task.news_id,
                    content=article.content,
                    content_hash=content_hash,
                    title=article.title,
                    author=article.author,
                    publish_date=article.publish_date,
                    language=article.language,
                    extracted_by=article.extractor,
                )
                completed += 1
            except Exception as error:
                self.repository.fail_article_extraction(
                    task.news_id,
                    f"{type(error).__name__}: {error}",
                )
                failed += 1
        return ExtractionRun(
            stale_recovered=stale_recovered,
            queued=len(tasks),
            claimed=claimed,
            completed=completed,
            failed=failed,
            skipped_duplicates=skipped,
        )

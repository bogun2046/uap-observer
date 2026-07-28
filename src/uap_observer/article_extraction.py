"""Article text extraction queue and Trafilatura adapter."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
from dataclasses import dataclass
from typing import Protocol

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
        trafilatura = self._module()
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            raise RuntimeError(f"Unable to download article: {url}")
        return self.extract_html(downloaded, url=url)

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
        if not result:
            raise RuntimeError("No readable article content found")
        payload = json.loads(result)
        content = "\n\n".join(
            line.strip()
            for line in str(payload.get("text") or "").splitlines()
            if line.strip()
        )
        if len(content) < self.minimum_characters:
            raise RuntimeError(
                f"Extracted content is too short: {len(content)} characters"
            )
        return ExtractedArticle(
            content=content,
            title=payload.get("title"),
            author=payload.get("author"),
            publish_date=payload.get("date"),
            language=payload.get("language"),
            extractor=self._extractor_id(),
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

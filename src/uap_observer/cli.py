"""Command-line interface for local development and automation."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from uap_observer.ai_analysis import AnalysisService, OpenAIAnalyzer
from uap_observer.article_extraction import ArticleExtractionService
from uap_observer.collectors.rss import RssCollector
from uap_observer.collectors.web_pages import AaroCollector
from uap_observer.config import Settings
from uap_observer.database import Database
from uap_observer.models import SourceType
from uap_observer.publishing import MarkdownPublisher
from uap_observer.repositories import Repository
from uap_observer.source_config import load_sources


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uap-observer")
    parser.add_argument(
        "--database",
        type=Path,
        help="SQLite database path (defaults to data/uap.db or UAP_DB_PATH).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Create or migrate the SQLite database.")
    subparsers.add_parser("db-status", help="Show migration and row-count status.")

    sync_parser = subparsers.add_parser(
        "sync-sources",
        help="Import version-controlled source definitions into SQLite.",
    )
    sync_parser.add_argument("--config", type=Path, help="Source JSON configuration path.")

    collect_parser = subparsers.add_parser(
        "collect-rss",
        help="Collect enabled RSS sources incrementally.",
    )
    collect_parser.add_argument("--config", type=Path, help="Source JSON configuration path.")
    collect_parser.add_argument("--source", help="Collect one source slug only.")
    collect_parser.add_argument("--limit", type=int, help="Maximum feed entries per source.")

    web_parser = subparsers.add_parser(
        "collect-web",
        help="Collect enabled official HTML release pages.",
    )
    web_parser.add_argument("--source", default="aaro-press-products")
    web_parser.add_argument("--limit", type=int, default=20)

    extraction_parser = subparsers.add_parser(
        "extract-articles",
        help="Download and extract queued article bodies.",
    )
    extraction_parser.add_argument("--limit", type=int, default=20)
    extraction_parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Include previously failed extraction tasks.",
    )

    analysis_parser = subparsers.add_parser(
        "analyze-articles",
        help="Analyze extracted articles with structured OpenAI output.",
    )
    analysis_parser.add_argument("--limit", type=int, default=10)
    analysis_parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Include previously failed analysis tasks.",
    )
    analysis_parser.add_argument(
        "--model",
        help="OpenAI model override (defaults to OPENAI_MODEL).",
    )

    publish_parser = subparsers.add_parser(
        "publish-markdown",
        help="Generate source-linked Markdown pages from completed analysis.",
    )
    publish_parser.add_argument(
        "--output",
        type=Path,
        default=Path("site/generated"),
        help="Output directory (defaults to site/generated).",
    )
    publish_parser.add_argument(
        "--date",
        help="Homepage date in YYYY-MM-DD format (defaults to local today).",
    )
    publish_parser.add_argument("--limit", type=int, default=1000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_environment()
    database = Database(args.database or settings.database_path, settings.migrations_path)

    if args.command == "init-db":
        applied = database.initialize()
        if applied:
            print(f"Initialized {database.path}; applied: {', '.join(applied)}")
        else:
            print(f"Database is current: {database.path}")
        return 0

    database.initialize()
    repository = Repository(database)

    if args.command in {"sync-sources", "collect-rss"}:
        config_path = args.config or settings.sources_path
        source_definitions = load_sources(config_path)
        for source in source_definitions:
            repository.upsert_source(source)
        print(f"Synced {len(source_definitions)} source(s) from {config_path}")

    if args.command == "sync-sources":
        return 0

    if args.command == "collect-rss":
        if args.limit is not None and args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        sources = repository.get_sources(
            source_type=SourceType.RSS,
            slug=args.source,
        )
        if args.source and not sources:
            raise SystemExit(f"Enabled RSS source not found: {args.source}")
        collector = RssCollector(repository)
        total_inserted = 0
        for source in sources:
            result = collector.collect(source, limit=args.limit)
            total_inserted += result.inserted
            if result.not_modified:
                print(f"{source.slug}: not modified")
            else:
                print(
                    f"{source.slug}: fetched={result.fetched} inserted={result.inserted} "
                    f"duplicates={result.duplicates} filtered={result.filtered} "
                    f"invalid={result.invalid}"
                )
        print(f"RSS collection complete; inserted={total_inserted}")
        return 0

    if args.command == "collect-web":
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        sources = repository.get_sources(
            source_type=SourceType.WEB_PAGE,
            slug=args.source,
        )
        if not sources:
            raise SystemExit(f"Enabled web-page source not found: {args.source}")
        result = AaroCollector(repository).collect(sources[0], limit=args.limit)
        if result.not_modified:
            print(f"{args.source}: not modified")
        else:
            print(
                f"{args.source}: fetched={result.fetched} inserted={result.inserted} "
                f"duplicates={result.duplicates} invalid={result.invalid}"
            )
        return 0

    if args.command == "extract-articles":
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        run = ArticleExtractionService(repository).run(
            limit=args.limit,
            retry_failed=args.retry_failed,
        )
        print(
            f"Article extraction complete; stale_recovered={run.stale_recovered} "
            f"queued={run.queued} claimed={run.claimed} "
            f"completed={run.completed} failed={run.failed} "
            f"skipped_duplicates={run.skipped_duplicates}"
        )
        return 0

    if args.command == "analyze-articles":
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        analyzer = OpenAIAnalyzer(
            model=args.model or settings.openai_model,
            reasoning_effort=settings.reasoning_effort,
        )
        run = AnalysisService(repository, analyzer).run(
            limit=args.limit,
            retry_failed=args.retry_failed,
        )
        print(
            f"AI analysis complete; stale_recovered={run.stale_recovered} "
            f"queued={run.queued} claimed={run.claimed} "
            f"completed={run.completed} failed={run.failed}"
        )
        return 0

    if args.command == "publish-markdown":
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        if args.date and len(args.date) != 10:
            raise SystemExit("--date must use YYYY-MM-DD format")
        result = MarkdownPublisher(repository, args.output).publish(
            today=args.date,
            limit=args.limit,
        )
        print(
            f"Markdown publishing complete; news_pages={result.news_pages} "
            f"events={result.event_count} output={result.output_directory}"
        )
        return 0

    status = database.status()
    print(f"Database: {database.path}")
    print(f"Schema version: {status.schema_version}")
    for table, count in status.row_counts.items():
        print(f"{table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

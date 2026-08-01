"""Command-line interface for local development and automation."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from collections.abc import Sequence
from pathlib import Path

from uap_observer.ai_analysis import AnalysisService, DeepSeekAnalyzer, OpenAIAnalyzer
from uap_observer.article_extraction import ArticleExtractionService
from uap_observer.collectors.rss import RssCollector
from uap_observer.collectors.web_pages import AaroCaseCollector, AaroCollector
from uap_observer.collectors.x_api import XApiCollector
from uap_observer.collectors.youtube_api import YouTubeApiCollector
from uap_observer.config import Settings
from uap_observer.database import Database
from uap_observer.entity_linking import EntityLinkingService
from uap_observer.export_snapshot import export_snapshot
from uap_observer.models import SourceType
from uap_observer.postgres_export import snapshot_to_sql
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
    subparsers.add_parser("source-status", help="Show source enablement and fetch health.")
    subparsers.add_parser("analysis-status", help="Show AI queue and API key status.")

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
    collect_parser.add_argument("--force", action="store_true", help="Collect even when the source interval has not elapsed.")

    web_parser = subparsers.add_parser(
        "collect-web",
        help="Collect enabled official HTML release pages.",
    )
    web_parser.add_argument("--source", default="aaro-press-products")
    web_parser.add_argument("--limit", type=int, default=20)
    web_parser.add_argument("--force", action="store_true", help="Collect even when the source interval has not elapsed.")

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
    extraction_parser.add_argument(
        "--retry-blocked",
        action="store_true",
        help="Also retry extraction tasks previously blocked with HTTP 403.",
    )

    analysis_parser = subparsers.add_parser(
        "analyze-articles",
        help="Analyze extracted articles with the configured AI provider.",
    )
    analysis_parser.add_argument("--limit", type=int, default=10)
    analysis_parser.add_argument(
        "--title-translation-limit",
        type=int,
        default=100,
        help="Maximum untranslated titles to process independently of article analysis.",
    )
    analysis_parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Include previously failed analysis tasks.",
    )

    x_parser = subparsers.add_parser("collect-x", help="Collect recent X posts with the X API v2.")
    x_parser.add_argument("--limit", type=int, default=30)
    x_parser.add_argument("--force", action="store_true", help="Collect even when the source interval has not elapsed.")
    x_parser.add_argument("--query", help="Override the X recent-search query.")
    youtube_parser = subparsers.add_parser("collect-youtube", help="Collect videos from configured YouTube channels.")
    youtube_parser.add_argument("--limit", type=int, default=10)
    youtube_parser.add_argument("--force", action="store_true")
    analysis_parser.add_argument(
        "--model",
        help="Model override (defaults to provider-specific environment setting).",
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

    link_parser = subparsers.add_parser(
        "link-entities",
        help="Create person/event relationships from completed AI analysis.",
    )
    link_parser.add_argument("--limit", type=int, default=1000)

    export_parser = subparsers.add_parser(
        "export-json",
        help="Export a reviewed JSON snapshot for PostgreSQL migration.",
    )
    export_parser.add_argument("--output", type=Path, required=True)

    sql_parser = subparsers.add_parser(
        "snapshot-to-sql",
        help="Convert a JSON snapshot into reviewable PostgreSQL INSERT SQL.",
    )
    sql_parser.add_argument("--input", type=Path, required=True)
    sql_parser.add_argument("--output", type=Path, required=True)
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

    def source_due(source) -> bool:
        if not source.last_success_at:
            return True
        try:
            last_success = datetime.fromisoformat(source.last_success_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        return datetime.now(timezone.utc) >= last_success + timedelta(hours=source.refresh_interval_hours)

    if args.command == "source-status":
        sources = repository.get_sources(enabled_only=False)
        if not sources:
            print("No sources configured.")
            return 0
        for source in sources:
            state = "enabled" if source.enabled else "disabled"
            last_fetch = source.last_fetched_at or "never"
            last_success = source.last_success_at or "never"
            error = source.last_error or "none"
            print(
                f"{source.slug}: {state} type={source.source_type.value} "
                f"last_fetch={last_fetch} last_success={last_success} error={error}"
                f" refresh_interval={source.refresh_interval_hours}h"
            )
        return 0

    if args.command == "analysis-status":
        counts = repository.get_pipeline_counts()
        key_name = "DEEPSEEK_API_KEY" if settings.ai_provider == "deepseek" else "OPENAI_API_KEY"
        print(f"AI_PROVIDER: {settings.ai_provider}")
        print(f"{key_name}: {'configured' if os.getenv(key_name) else 'not configured'}")
        for status in ("pending", "processing", "completed", "failed", "skipped"):
            print(f"{status}: {counts.get(status, 0)}")
        return 0

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
        failed_sources = 0
        for source in sources:
            if not args.force and not source_due(source):
                print(f"{source.slug}: skipped (next refresh in {source.refresh_interval_hours}h interval)")
                continue
            try:
                result = collector.collect(source, limit=args.limit)
            except Exception as error:  # noqa: BLE001
                failed_sources += 1
                print(f"{source.slug}: collection failed: {type(error).__name__}: {error}")
                continue
            total_inserted += result.inserted
            if result.not_modified:
                print(f"{source.slug}: not modified")
            else:
                print(
                    f"{source.slug}: fetched={result.fetched} inserted={result.inserted} "
                    f"duplicates={result.duplicates} filtered={result.filtered} "
                    f"invalid={result.invalid}"
                )
        print(
            f"RSS collection complete; inserted={total_inserted} "
            f"failed_sources={failed_sources}"
        )
        if args.source and failed_sources:
            return 1
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
        if not args.force and not source_due(sources[0]):
            print(f"{args.source}: skipped (next refresh in {sources[0].refresh_interval_hours}h interval)")
            return 0
        collector = (
            AaroCaseCollector(repository)
            if args.source == "aaro-case-resolutions"
            else AaroCollector(repository)
        )
        result = collector.collect(sources[0], limit=args.limit)
        if result.not_modified:
            print(f"{args.source}: not modified")
        else:
            print(
                f"{args.source}: fetched={result.fetched} inserted={result.inserted} "
                f"duplicates={result.duplicates} invalid={result.invalid} "
                f"events={result.events_inserted}"
            )
        return 0

    if args.command == "collect-x":
        if args.limit < 10 or args.limit > 100:
            raise SystemExit("--limit must be between 10 and 100")
        sources = repository.get_sources(source_type=SourceType.API, slug="x-uap")
        if not sources:
            raise SystemExit("Enabled X source not found; run sync-sources first")
        if not args.force and not source_due(sources[0]):
            print(f"x-uap: skipped (next refresh in {sources[0].refresh_interval_hours}h interval)")
            return 0
        result = XApiCollector(repository).collect(
            sources[0], limit=args.limit, query=args.query
        )
        print(
            f"X collection complete; fetched={result.fetched} inserted={result.inserted} "
            f"duplicates={result.duplicates}"
        )
        return 0

    if args.command == "collect-youtube":
        if args.limit < 1 or args.limit > 50:
            raise SystemExit("--limit must be between 1 and 50")
        sources = repository.get_sources(source_type=SourceType.API, slug="youtube-uap")
        if not sources:
            raise SystemExit("Enabled YouTube source not found; run sync-sources first")
        if not args.force and not source_due(sources[0]):
            print(f"youtube-uap: skipped (next refresh in {sources[0].refresh_interval_hours}h interval)")
            return 0
        result = YouTubeApiCollector(repository).collect(sources[0], limit=args.limit)
        print(
            f"YouTube collection complete; channels={result.channels} fetched={result.fetched} "
            f"inserted={result.inserted} duplicates={result.duplicates} priority={result.priority}"
        )
        return 0

    if args.command == "extract-articles":
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        run = ArticleExtractionService(repository).run(
            limit=args.limit,
            retry_failed=args.retry_failed,
            retry_blocked=args.retry_blocked,
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
        model = args.model or (
            settings.deepseek_model
            if settings.ai_provider == "deepseek"
            else settings.openai_model
        )
        analyzer = (
            DeepSeekAnalyzer(
                model=model,
                api_key=os.getenv("DEEPSEEK_API_KEY"),
                reasoning_effort=settings.reasoning_effort,
            )
            if settings.ai_provider == "deepseek"
            else OpenAIAnalyzer(model=model, reasoning_effort=settings.reasoning_effort)
        )
        run = AnalysisService(repository, analyzer).run(
            limit=args.limit,
            retry_failed=args.retry_failed,
            title_translation_limit=args.title_translation_limit,
        )
        print(
            f"AI analysis complete; stale_recovered={run.stale_recovered} "
            f"queued={run.queued} claimed={run.claimed} "
            f"completed={run.completed} failed={run.failed} "
            f"titles_translated={run.titles_translated}"
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

    if args.command == "link-entities":
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        run = EntityLinkingService(repository).run(limit=args.limit)
        print(
            f"Entity linking complete; records={run.records} "
            f"persons_created={run.persons_created} events_created={run.events_created} "
            f"organizations_created={run.organizations_created} "
            f"organizations_normalized={run.organizations_normalized} "
            f"relationships_created={run.relationships_created} "
            f"skipped_invalid={run.skipped_invalid}"
        )
        return 0

    if args.command == "export-json":
        rows = export_snapshot(database, args.output)
        print(f"SQLite snapshot exported; rows={rows} output={args.output}")
        return 0

    if args.command == "snapshot-to-sql":
        rows = snapshot_to_sql(args.input, args.output)
        print(f"PostgreSQL SQL generated; rows={rows} output={args.output}")
        return 0

    status = database.status()
    print(f"Database: {database.path}")
    print(f"Schema version: {status.schema_version}")
    for table, count in status.row_counts.items():
        print(f"{table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

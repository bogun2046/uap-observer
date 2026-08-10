"""Command-line interface for local development and automation."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from uap_observer.ai_analysis import (
    AnalysisService,
    DeepSeekAnalyzer,
    OpenAIAnalyzer,
    safe_provider_error,
)
from uap_observer.article_extraction import ArticleExtractionService
from uap_observer.collectors.rss import RssCollector
from uap_observer.collectors.web_pages import AaroCaseCollector, AaroCollector
from uap_observer.collectors.x_api import XApiCollector
from uap_observer.collectors.youtube_api import YouTubeApiCollector
from uap_observer.collectors.youtube_captions import YouTubeCaptionCollector
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
    deepseek_health_parser = subparsers.add_parser(
        "deepseek-health-check",
        help="Validate the DeepSeek API key, connection, and configured model.",
    )
    deepseek_health_parser.add_argument(
        "--model",
        help="DeepSeek model override (defaults to DEEPSEEK_MODEL).",
    )

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
    extraction_parser.add_argument(
        "--max-failed-attempts",
        type=int,
        default=3,
        help="Maximum total attempts for automatically retried failed tasks (default: 3).",
    )
    extraction_parser.add_argument(
        "--force-retry-exhausted",
        action="store_true",
        help="Ignore the failed-task attempt limit for this explicit recovery run.",
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
    captions_parser = subparsers.add_parser("collect-youtube-transcripts", help="Collect captions for priority YouTube videos.")
    captions_parser.add_argument("--limit", type=int, default=5)
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

    if args.command == "deepseek-health-check":
        model = args.model or settings.deepseek_model
        analyzer = DeepSeekAnalyzer(
            model=model,
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            reasoning_effort=settings.reasoning_effort,
        )
        try:
            health = analyzer.health_check()
        except Exception as error:  # noqa: BLE001 - CLI boundary with sanitized output
            print(
                "DeepSeek health check failed: "
                f"{safe_provider_error(error, provider='DeepSeek')}"
            )
            return 1
        print(
            "DeepSeek health check OK; "
            f"model={health.model} available_models={health.available_models}"
        )
        return 0

    database.initialize()
    repository = Repository(database)

    def source_due(source) -> bool:
        if source.next_retry_at:
            try:
                retry_at = datetime.fromisoformat(source.next_retry_at.replace("Z", "+00:00"))
            except ValueError:
                retry_at = None
            if retry_at and datetime.now(timezone.utc) < retry_at:
                return False
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
        latest_runs = repository.get_latest_source_runs()
        for source in sources:
            state = "enabled" if source.enabled else "disabled"
            last_fetch = source.last_fetched_at or "never"
            last_success = source.last_success_at or "never"
            error = source.last_error or "none"
            line = (
                f"{source.slug}: {state} type={source.source_type.value} "
                f"last_fetch={last_fetch} last_success={last_success} error={error}"
                f" refresh_interval={source.refresh_interval_hours}h"
                f" next_retry={source.next_retry_at or 'none'}"
                f" consecutive_failures={source.consecutive_failures}"
            )
            run = latest_runs.get(int(source.id)) if source.id is not None else None
            if run:
                line += (
                    f" last_run={run['status']}"
                    f" fetched={run['fetched_count']}"
                    f" parsed={run['parsed_count']}"
                    f" inserted={run['inserted_count']}"
                    f" duplicates={run['duplicate_count']}"
                    f" filtered={run['filtered_count']}"
                    f" invalid={run['invalid_count']}"
                )
                if run.get("error"):
                    line += f" run_error={run['error']}"
            else:
                line += " last_run=never"
            print(line)
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
                if source.next_retry_at:
                    print(f"{source.slug}: skipped (cooldown until {source.next_retry_at})")
                else:
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
            if sources[0].next_retry_at:
                print(f"{args.source}: skipped (cooldown until {sources[0].next_retry_at})")
            else:
                print(f"{args.source}: skipped (next refresh in {sources[0].refresh_interval_hours}h interval)")
            return 0
        collector = (
            AaroCaseCollector(repository)
            if args.source == "aaro-case-resolutions"
            else AaroCollector(repository)
        )
        try:
            result = collector.collect(sources[0], limit=args.limit)
        except Exception as error:  # noqa: BLE001 - CLI boundary for persisted collector failures
            # The collector has already persisted the failed source run. Keep
            # scheduled/manual collection output concise and actionable.
            print(f"{args.source}: collection failed: {type(error).__name__}: {error}")
            return 1
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

    if args.command == "collect-youtube-transcripts":
        if args.limit < 1 or args.limit > 20:
            raise SystemExit("--limit must be between 1 and 20")
        result = YouTubeCaptionCollector(repository, max_videos=args.limit).collect()
        print(
            f"YouTube transcripts complete; requested={result.requested} completed={result.completed} "
            f"skipped={result.skipped} failed={result.failed} estimated_tokens={result.token_count}"
        )
        return 0

    if args.command == "extract-articles":
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        if args.max_failed_attempts < 1:
            raise SystemExit("--max-failed-attempts must be at least 1")
        if args.force_retry_exhausted and not args.retry_failed:
            raise SystemExit("--force-retry-exhausted requires --retry-failed")
        run = ArticleExtractionService(repository).run(
            limit=args.limit,
            retry_failed=args.retry_failed,
            retry_blocked=args.retry_blocked,
            max_failed_attempts=(
                None if args.force_retry_exhausted else args.max_failed_attempts
            ),
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
        if args.title_translation_limit < 0:
            raise SystemExit("--title-translation-limit must be zero or greater")
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
            f"titles_translated={run.titles_translated} "
            f"titles_failed={run.titles_failed}"
        )
        for failure in run.failures:
            print(
                f"AI item failed; stage={failure.stage} "
                f"news_id={failure.news_id} "
                f"provider_attempts={failure.attempts} "
                f"error={failure.error} "
                f"response_id={failure.response_id or 'none'}"
            )
        if run.provider_access_failed:
            print(f"AI analysis stopped: {run.fatal_error}")
            return 1
        if run.titles_failed:
            print(
                "AI analysis stopped: title translation failures remain; "
                "public publishing is blocked."
            )
            return 1
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
            f"tags_created={run.tags_created} "
            f"person_relationships_created={run.person_relationships_created} "
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

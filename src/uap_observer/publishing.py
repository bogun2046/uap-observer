"""Generate source-linked Markdown pages from completed database records."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from uap_observer.repositories import Repository


@dataclass(frozen=True)
class PublishResult:
    news_pages: int
    event_count: int
    output_directory: Path
    person_count: int = 0
    relationship_count: int = 0


class MarkdownPublisher:
    """Build deterministic Markdown output without copying article bodies."""

    def __init__(self, repository: Repository, output_directory: Path) -> None:
        self.repository = repository
        self.output_directory = Path(output_directory)

    def publish(self, *, today: str | None = None, limit: int = 1000) -> PublishResult:
        publish_date = today or datetime.now(timezone.utc).date().isoformat()
        news = self.repository.get_published_news(limit=limit)
        events = self.repository.get_events_for_timeline(limit=limit)
        persons = self.repository.get_persons(limit=limit)
        relationships = self.repository.get_relationships(limit=limit * 2)
        news_directory = self.output_directory / "news"
        events_directory = self.output_directory / "events"
        news_directory.mkdir(parents=True, exist_ok=True)
        events_directory.mkdir(parents=True, exist_ok=True)

        pages: list[tuple[str, str]] = []
        for row in news:
            filename = _news_filename(row)
            pages.append(
                (
                    filename,
                    _render_news_detail(
                        row,
                        self.repository.get_news_entities(int(row["id"])),
                    ),
                )
            )
        for filename, content in pages:
            (news_directory / filename).write_text(content, encoding="utf-8")

        today_news = [row for row in news if _date_prefix(row.get("publish_date")) == publish_date]
        (self.output_directory / "index.md").write_text(
            _render_home(today_news, publish_date),
            encoding="utf-8",
        )
        (news_directory / "index.md").write_text(
            _render_news_index(news),
            encoding="utf-8",
        )
        (events_directory / "index.md").write_text(
            _render_events_index(events),
            encoding="utf-8",
        )
        (self.output_directory / "timeline.md").write_text(
            _render_timeline(events),
            encoding="utf-8",
        )
        (self.output_directory / "persons").mkdir(parents=True, exist_ok=True)
        (self.output_directory / "persons" / "index.md").write_text(
            _render_persons_index(persons),
            encoding="utf-8",
        )
        (self.output_directory / "relationships.md").write_text(
            _render_relationships(relationships),
            encoding="utf-8",
        )
        return PublishResult(
            news_pages=len(pages),
            event_count=len(events),
            output_directory=self.output_directory,
            person_count=len(persons),
            relationship_count=len(relationships),
        )


def _render_home(rows: list[dict[str, object]], today: str) -> str:
    lines = ["---", 'title: "今日UAP新闻"', "layout: default", "---", "", f"更新时间：{today}", ""]
    if not rows:
        lines.append("今日暂无已完成 AI 分析的新闻。")
        lines.append("")
        lines.append("[查看全部新闻](news/index.md)")
        return "\n".join(lines) + "\n"
    lines.extend(_render_news_cards(rows, link_prefix="news/"))
    lines.extend(("", "[查看全部新闻](news/index.md)", ""))
    return "\n".join(lines)


def _render_news_index(rows: list[dict[str, object]]) -> str:
    lines = ["---", 'title: "UAP新闻"', "layout: default", "---", ""]
    if not rows:
        lines.extend(("暂无已完成分析的新闻。", ""))
        return "\n".join(lines)
    lines.extend(_render_news_cards(rows, link_prefix=""))
    return "\n".join(lines)


def _render_news_cards(
    rows: Iterable[dict[str, object]],
    *,
    link_prefix: str,
) -> list[str]:
    lines: list[str] = []
    for row in rows:
        filename = _news_filename(row)
        title = _text(row.get("title")) or _text(row.get("original_title")) or "未命名新闻"
        summary = _text(row.get("summary")) or "暂无摘要。"
        date_value = _date_prefix(row.get("publish_date")) or "日期未知"
        credibility = _stars(row.get("credibility"))
        lines.extend(
            (
                f"## [{_escape_text(title)}]({link_prefix}{filename})",
                "",
                f"{credibility}　来源：{_escape_text(_text(row.get('source')) or '未知来源')}　日期：{date_value}",
                "",
                _escape_text(summary),
                "",
            )
        )
    return lines


def _render_news_detail(row: dict[str, object], entities: list[dict[str, object]]) -> str:
    title = _text(row.get("title")) or _text(row.get("original_title")) or "未命名新闻"
    facts = _json_list(row.get("key_facts"))
    viewpoints = _json_list(row.get("viewpoints"))
    lines = [
        "---",
        f'title: "{_yaml_text(title)}"',
        "layout: default",
        f"source: {_yaml_text(_text(row.get('source')))}",
        "---",
        "",
        f"# {_escape_text(title)}",
        "",
        f"原标题：{_escape_text(_text(row.get('original_title')))}",
        f"来源：[{_escape_text(_text(row.get('source')))}](<{_text(row.get('source_url'))}>)",
        f"发布时间：{_date_prefix(row.get('publish_date')) or '未知'}",
        f"可信度：{_stars(row.get('credibility'))}",
        f"事实状态：`{_escape_text(_text(row.get('fact_status')) or 'unknown')}`",
        "",
        "## AI摘要",
        "",
        _escape_text(_text(row.get("summary")) or "暂无摘要。"),
        "",
        "## 关键事实",
        "",
    ]
    lines.extend([f"- {_escape_text(item)}" for item in facts] or ["- 暂无。"])
    lines.extend(("", "## 不同观点", ""))
    lines.extend([f"- {_escape_text(item)}" for item in viewpoints] or ["- 文中未识别到不同观点。"])
    lines.extend(("", "## 相关人物与事件", ""))
    if entities:
        for entity in entities:
            name = _text(entity.get("entity_name")) or _text(entity.get("event_name"))
            kind = "人物" if entity.get("entity_type") == "person" else "事件"
            lines.append(f"- {kind}：{_escape_text(name)}")
    else:
        lines.append("- 暂无已建立的关系。")
    lines.extend(("", "## 分析信息", "", f"- 模型：`{_escape_text(_text(row.get('ai_model')) or 'unknown')}`"))
    if row.get("analysis_confidence") is not None:
        lines.append(f"- 分析置信度：{float(row['analysis_confidence']):.2f}")
    lines.extend(("", "原文请访问上方来源链接。本站不转载抓取的文章正文。", ""))
    return "\n".join(lines)


def _render_events_index(events: list[dict[str, object]]) -> str:
    lines = ["---", 'title: "历史UAP事件"', "layout: default", "---", ""]
    if not events:
        lines.extend(("暂无已录入的历史事件。", ""))
        return "\n".join(lines)
    for event in events:
        period = _date_prefix(event.get("date_start")) or "日期未知"
        if event.get("date_end"):
            period += f"—{_date_prefix(event.get('date_end'))}"
        description = _text(event.get("description")) or "暂无描述。"
        lines.extend((f"## {_escape_text(_text(event.get('event_name')))}", "", f"时间：{period}"))
        if event.get("location") or event.get("country"):
            location = "，".join(filter(None, (_text(event.get("location")), _text(event.get("country")))))
            lines.append(f"地点：{_escape_text(location)}")
        lines.extend(("", _escape_text(description), ""))
    return "\n".join(lines)


def _render_timeline(events: list[dict[str, object]]) -> str:
    lines = ["---", 'title: "UAP时间线"', "layout: default", "---", ""]
    if not events:
        lines.extend(("暂无已录入的时间线事件。", ""))
        return "\n".join(lines)
    for event in events:
        year = (_date_prefix(event.get("date_start")) or "未知")[:4]
        name = _escape_text(_text(event.get("event_name")) or "未命名事件")
        lines.append(f"- **{year}**　{name}")
    lines.append("")
    return "\n".join(lines)


def _render_persons_index(persons: list[dict[str, object]]) -> str:
    lines = ["---", 'title: "人物"', "layout: default", "---", ""]
    if not persons:
        return "\n".join(lines + ["暂无已建立的人物实体。", ""])
    for person in persons:
        name = _escape_text(_text(person.get("name")) or "未命名人物")
        organization = _text(person.get("organization"))
        lines.extend((f"## {name}", ""))
        if organization:
            lines.append(f"机构：{_escape_text(organization)}")
        if person.get("country"):
            lines.append(f"国家/地区：{_escape_text(_text(person.get('country')))}")
        lines.extend(("", _escape_text(_text(person.get("description")) or "暂无描述。"), ""))
    return "\n".join(lines)


def _render_relationships(relationships: list[dict[str, object]]) -> str:
    lines = ["---", 'title: "人物与事件关系"', "layout: default", "---", ""]
    if not relationships:
        return "\n".join(lines + ["暂无已建立的关系。", ""])
    lines.extend(("| 新闻 | 关系 | 实体 | 置信度 |", "| --- | --- | --- | --- |"))
    for relationship in relationships:
        entity = _text(relationship.get("person_name")) or _text(relationship.get("event_name"))
        confidence = relationship.get("confidence")
        confidence_text = f"{float(confidence):.2f}" if confidence is not None else "—"
        lines.append(
            "| "
            f"{_escape_table(_text(relationship.get('news_title')))} | "
            f"{_escape_table(_text(relationship.get('relationship_type')))} | "
            f"{_escape_table(entity)} | {confidence_text} |"
        )
    lines.append("")
    return "\n".join(lines)


def _news_filename(row: dict[str, object]) -> str:
    title = _text(row.get("title")) or _text(row.get("original_title")) or "news"
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", title.lower()).strip("-")[:70]
    return f"{int(row['id'])}-{slug or 'news'}.md"


def _json_list(value: object) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed if str(item).strip()] if isinstance(parsed, list) else []


def _date_prefix(value: object) -> str:
    return _text(value)[:10] if value else ""


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _stars(value: object) -> str:
    try:
        score = min(5, max(1, int(value)))
    except (TypeError, ValueError):
        score = 1
    return "★" * score + "☆" * (5 - score)


def _escape_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("`", "\\`").replace("\n", " ")


def _yaml_text(value: str) -> str:
    return _escape_text(value).replace('"', '\\"')


def _escape_table(value: str) -> str:
    return _escape_text(value).replace("|", "\\|")

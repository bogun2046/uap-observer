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
        organizations = self.repository.get_organizations(limit=limit)
        relationships = self.repository.get_relationships(limit=limit * 2)
        news_directory = self.output_directory / "news"
        events_directory = self.output_directory / "events"
        news_directory.mkdir(parents=True, exist_ok=True)
        events_directory.mkdir(parents=True, exist_ok=True)
        self._write_jekyll_support_files()

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
            for alias in _legacy_news_filenames(row):
                pages.append((alias, _render_news_redirect(filename)))
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
        (self.output_directory / "search.json").write_text(
            json.dumps(_render_search_index(news), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.output_directory / "search.md").write_text(
            _render_search_page(),
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
        (self.output_directory / "organizations.md").write_text(
            _render_organizations(organizations),
            encoding="utf-8",
        )
        (self.output_directory / "relationships.md").write_text(
            _render_relationships(relationships),
            encoding="utf-8",
        )
        return PublishResult(
            news_pages=len(news),
            event_count=len(events),
            output_directory=self.output_directory,
            person_count=len(persons),
            relationship_count=len(relationships),
        )

    def _write_jekyll_support_files(self) -> None:
        (self.output_directory / "_layouts").mkdir(parents=True, exist_ok=True)
        (self.output_directory / "_config.yml").write_text(
            "markdown: kramdown\nrelative_links:\n  enabled: true\n",
            encoding="utf-8",
        )
        (self.output_directory / "_layouts" / "default.html").write_text(
            """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ page.title | default: 'UAP Observer' }}</title>
  <style>body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;line-height:1.6}a{color:#145da0}table{border-collapse:collapse}th,td{border:1px solid #ddd;padding:.4rem .6rem;text-align:left}</style>
</head>
<body>{{ content }}</body>
</html>
""",
            encoding="utf-8",
        )


def _render_home(rows: list[dict[str, object]], today: str) -> str:
    lines = ["---", 'title: "今日UAP新闻"', "layout: default", "---", "", f"更新时间：{today}", ""]
    if not rows:
        lines.append("今日暂无已完成 AI 分析的新闻。")
        lines.append("")
        lines.append("[查看全部新闻](news/index.md) · [搜索新闻](search.md)")
        return "\n".join(lines) + "\n"
    lines.extend(_render_news_cards(rows, link_prefix="news/"))
    lines.extend(("", "[查看全部新闻](news/index.md) · [搜索新闻](search.md)", ""))
    return "\n".join(lines)


def _render_news_index(rows: list[dict[str, object]]) -> str:
    lines = [
        "---",
        'title: "UAP新闻"',
        "layout: default",
        "---",
        "",
        "[搜索新闻](../search.md)",
        "",
    ]
    if not rows:
        lines.extend(("暂无已完成分析的新闻。", ""))
        return "\n".join(lines)
    lines.extend(("按分类浏览：", ""))
    categories: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        category = _text(row.get("category")) or "other"
        categories.setdefault(category, []).append(row)
    for category, category_rows in categories.items():
        lines.extend((f"## {_escape_text(_category_label(category))}", ""))
        lines.extend(_render_news_cards(category_rows, link_prefix=""))
    return "\n".join(lines)


def _render_search_index(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return a small public index containing metadata, never article bodies."""
    return [
        {
            "id": int(row["id"]),
            "title": _text(row.get("title")) or _text(row.get("original_title")),
            "summary": _text(row.get("summary")),
            "source": _text(row.get("source")),
            "publish_date": _date_prefix(row.get("publish_date")),
            "category": _text(row.get("category")) or "other",
            "fact_status": _text(row.get("fact_status")),
            "credibility": row.get("credibility"),
            "url": f"news/{_news_filename(row)}",
        }
        for row in rows
    ]


def _render_search_page() -> str:
    return """---
title: "搜索UAP新闻"
layout: default
---

<input id="uap-search" type="search" placeholder="搜索标题、摘要、来源或分类" style="width:100%;max-width:42rem;padding:.6rem" />
<div id="uap-search-results" aria-live="polite">正在加载索引……</div>

<script>
(() => {
  const input = document.getElementById('uap-search');
  const results = document.getElementById('uap-search-results');
  const escapeHtml = value => String(value ?? '').replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
  fetch('search.json').then(r => r.json()).then(items => {
    const render = () => {
      const query = input.value.trim().toLowerCase();
      const matches = items.filter(item =>
        [item.title, item.summary, item.source, item.category].join(' ').toLowerCase().includes(query));
      results.innerHTML = matches.length
        ? matches.map(item => `<article><h2><a href="${escapeHtml(item.url)}">${escapeHtml(item.title || '未命名新闻')}</a></h2><p>${escapeHtml(item.publish_date || '日期未知')} · ${escapeHtml(item.source || '未知来源')}</p><p>${escapeHtml(item.summary || '暂无摘要。')}</p></article>`).join('')
        : '<p>没有匹配的新闻。</p>';
    };
    input.addEventListener('input', render);
    render();
  }).catch(() => { results.textContent = '搜索索引暂时不可用。'; });
})();
</script>
"""


def _category_label(category: str) -> str:
    labels = {
        "official_report": "官方报告",
        "government_document": "政府文件",
        "military": "军事相关",
        "scientific_research": "科学研究",
        "historical_event": "历史事件",
        "sighting": "目击事件",
        "disputed_event": "争议事件",
        "other": "其他",
    }
    return labels.get(category, category)


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
        f"分析状态：`{_escape_text(_text(row.get('processing_status')) or 'pending')}`",
        f"正文提取状态：`{_escape_text(_text(row.get('extraction_status')) or 'pending')}`",
        "",
        "## AI摘要",
        "",
        _escape_text(_ai_summary_or_status_message(row)),
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
            name = (
                _text(entity.get("entity_name"))
                or _text(entity.get("event_name"))
                or _text(entity.get("organization_name"))
            )
            kind = {"person": "人物", "event": "事件", "organization": "机构"}.get(
                str(entity.get("entity_type")), "实体"
            )
            lines.append(f"- {kind}：{_escape_text(name)}")
    else:
        lines.append("- 暂无已建立的关系。")
    lines.extend(("", "## 分析信息", "", f"- 模型：`{_escape_text(_text(row.get('ai_model')) or 'unknown')}`"))
    if row.get("analysis_confidence") is not None:
        lines.append(f"- 分析置信度：{float(row['analysis_confidence']):.2f}")
    lines.extend(("", "原文请访问上方来源链接。本站不转载抓取的文章正文。", ""))
    return "\n".join(lines)


def _analysis_unavailable_message(row: dict[str, object]) -> str:
    extraction_status = _text(row.get("extraction_status")) or "pending"
    if extraction_status == "failed":
        return "原文正文提取失败，暂无法生成有依据的 AI 摘要；请访问来源链接查看原文。"
    if extraction_status != "completed":
        return "原文正文尚未提取，AI 摘要将在正文提取完成后生成。"
    return "该新闻已通过来源筛选，AI 摘要尚未生成。"


def _ai_summary_or_status_message(row: dict[str, object]) -> str:
    """Only display a summary as AI output after analysis is complete."""

    if _text(row.get("processing_status")) == "completed":
        return _text(row.get("summary")) or "AI 摘要为空。"
    return _analysis_unavailable_message(row)


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
        entity = (
            _text(relationship.get("person_name"))
            or _text(relationship.get("event_name"))
            or _text(relationship.get("organization_name"))
        )
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


def _render_organizations(organizations: list[dict[str, object]]) -> str:
    lines = ["---", 'title: "机构"', "layout: default", "---", ""]
    if not organizations:
        return "\n".join(lines + ["暂无已建立的机构实体。", ""])
    for organization in organizations:
        lines.extend(
            (
                f"## {_escape_text(_text(organization.get('name')) or '未命名机构')}",
                "",
                _escape_text(_text(organization.get("description")) or "暂无描述。"),
                "",
            )
        )
    return "\n".join(lines)


def _news_filename(row: dict[str, object]) -> str:
    """Use an immutable ID so translated titles never change the public URL."""

    return f"{int(row['id'])}.md"


def _legacy_news_filenames(row: dict[str, object]) -> list[str]:
    """Return old title-based filenames as compatibility redirects."""

    filenames: list[str] = []
    for value in (row.get("original_title"), row.get("title")):
        title = _text(value)
        slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", title.lower()).strip("-")[:70]
        if not slug:
            continue
        filename = f"{int(row['id'])}-{slug}.md"
        if filename != _news_filename(row) and filename not in filenames:
            filenames.append(filename)
    return filenames


def _render_news_redirect(target_filename: str) -> str:
    return "\n".join(
        (
            "---",
            'title: "文章链接已更新"',
            "layout: default",
            "---",
            "",
            f'<meta http-equiv="refresh" content="0; url=../news/{target_filename}">',
            f"文章链接已更新，请访问 [最新页面](../news/{target_filename})。",
            "",
        )
    )


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

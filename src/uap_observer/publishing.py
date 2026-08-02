"""Generate source-linked Markdown pages from completed database records."""

from __future__ import annotations

import html
import json
import re
import shutil
from collections import Counter
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
        events = self.repository.get_events(limit=limit)
        persons = self.repository.get_persons(limit=limit)
        organizations = self.repository.get_organizations(limit=limit)
        relationships = self.repository.get_relationships(limit=limit * 2)
        sources = self.repository.get_sources(enabled_only=False)
        news_entities = {
            int(row["id"]): self.repository.get_news_entities(int(row["id"]))
            for row in news
        }
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

        (self.output_directory / "index.md").write_text(
            _render_home(news, events, sources, publish_date),
            encoding="utf-8",
        )
        (news_directory / "index.md").write_text(
            _render_news_index(news, news_entities),
            encoding="utf-8",
        )
        (self.output_directory / "search.json").write_text(
            json.dumps(_render_search_index(news, news_entities), ensure_ascii=False, indent=2) + "\n",
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
            _render_timeline([event for event in events if event.get("date_start")]),
            encoding="utf-8",
        )
        (self.output_directory / "persons").mkdir(parents=True, exist_ok=True)
        person_news = {
            int(person["id"]): self.repository.get_entity_news(entity_type="person", entity_id=int(person["id"]))
            for person in persons
        }
        organization_news = {
            int(organization["id"]): self.repository.get_entity_news(
                entity_type="organization", entity_id=int(organization["id"])
            )
            for organization in organizations
        }
        (self.output_directory / "persons" / "index.md").write_text(
            _render_persons_index(persons, person_news),
            encoding="utf-8",
        )
        (self.output_directory / "organizations.md").write_text(
            _render_organizations(organizations, organization_news),
            encoding="utf-8",
        )
        (self.output_directory / "tags.md").write_text(
            _render_tags_index(persons, organizations, person_news, organization_news),
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
        assets_directory = self.output_directory / "assets"
        assets_directory.mkdir(parents=True, exist_ok=True)
        resources_directory = Path(__file__).parent / "resources"
        shutil.copyfile(resources_directory / "site.css", assets_directory / "site.css")
        shutil.copyfile(resources_directory / "site.js", assets_directory / "site.js")
        silver_hero = resources_directory / "silver-metal-background-hero.png"
        if silver_hero.exists():
            shutil.copyfile(silver_hero, assets_directory / "silver-metal-background-hero.png")
        og_image = resources_directory / "og.png"
        if og_image.exists():
            shutil.copyfile(og_image, assets_directory / "og.png")
        world_map = resources_directory / "world-map.svg"
        if world_map.exists():
            shutil.copyfile(world_map, assets_directory / "world-map.svg")
        (self.output_directory / "_config.yml").write_text(
            "title: UAP Observer\n"
            "description: 开放、可追溯的 UAP 信息档案\n"
            "markdown: kramdown\n"
            "relative_links:\n"
            "  enabled: true\n",
            encoding="utf-8",
        )
        (self.output_directory / "_layouts" / "default.html").write_text(
            """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#06101c">
  <meta name="description" content="{{ page.description | default: site.description | escape }}">
  <meta property="og:title" content="{{ page.title | default: site.title | escape }}">
  <meta property="og:description" content="{{ page.description | default: site.description | escape }}">
  <meta property="og:type" content="website">
  <meta property="og:image" content="{{ '/assets/og.png' | absolute_url }}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:image" content="{{ '/assets/og.png' | absolute_url }}">
  <title>{{ page.title | default: site.title }} · UAP Observer</title>
  <link rel="stylesheet" href="{{ '/assets/site.css' | relative_url }}">
</head>
<body class="{{ page.page_kind | default: 'content-page' }}">
  <header class="site-header">
    <a class="wordmark" href="{{ '/' | relative_url }}" aria-label="UAP Observer 首页">
      <span class="wordmark-mark" aria-hidden="true"><i></i></span>
      <span>UAP OBSERVER</span>
    </a>
    <nav aria-label="主导航">
      <a href="{{ '/news/' | relative_url }}">每日更新</a>
      <a href="{{ '/events/' | relative_url }}">事件档案</a>
      <a href="{{ '/timeline.html' | relative_url }}">时间线</a>
      <a href="{{ '/search.html' | relative_url }}">搜索</a>
    </nav>
    <span class="archive-status"><i aria-hidden="true"></i> 开放档案</span>
  </header>
  <main class="{% if page.page_kind == 'home' %}home-shell{% else %}content-shell{% endif %}">
    {{ content }}
  </main>
  <footer class="site-footer">
    <div class="footer-brand"><span class="wordmark-mark" aria-hidden="true"><i></i></span><strong>UAP OBSERVER</strong></div>
    <p>开放记录未知现象，不预设结论。</p>
    <div><a href="{{ '/tags.html' | relative_url }}">标签</a><a href="{{ '/relationships.html' | relative_url }}">关系</a><a href="{{ '/organizations.html' | relative_url }}">机构</a></div>
  </footer>
  <script src="{{ '/assets/site.js' | relative_url }}" defer></script>
</body>
</html>
""",
            encoding="utf-8",
        )


def _render_home(
    news: list[dict[str, object]],
    events: list[dict[str, object]],
    sources: list[object],
    today: str,
) -> str:
    sync_values = [
        str(getattr(source, "last_success_at", "") or "")
        for source in sources
        if getattr(source, "last_success_at", None)
    ]
    last_sync = max(sync_values) if sync_values else today
    category_counts = Counter(_text(row.get("category")) or "other" for row in news)
    country_counts = Counter(_text(event.get("country")) for event in events if _text(event.get("country")))
    missing_location_count = sum(1 for event in events if not _text(event.get("country")))

    lines = [
        "---",
        'title: "开放观测档案"',
        'description: "独立整理公开 UAP 信息，以可追溯来源、时间与事实状态建立开放档案。"',
        "layout: default",
        "page_kind: home",
        "---",
        "",
        '<section class="hero" id="top">',
        '  <div class="hero-copy hero-enter">',
        '    <p class="eyebrow">SOURCE-FIRST OPEN ARCHIVE</p>',
        "    <h1>让未知，<br>留下证据。</h1>",
        "    <p class=\"hero-description\">持续整理公开 UAP 信息，以可追溯来源、时间与事实状态建立开放档案。</p>",
        '    <div class="hero-actions"><a class="button button-primary" href="#event-map">进入事件地图 <span aria-hidden="true">↗</span></a><a class="button button-secondary" href="#method">查看方法</a></div>',
        f'    <a class="daily-update-entry" href="news/index.html" aria-label="打开每日更新，最近同步 {_html(_display_datetime(last_sync))}"><span><strong>每日更新</strong><small>最近同步 {_html(_display_datetime(last_sync))}，共 {len(news)} 条来源记录</small></span><span class="daily-update-arrow" aria-hidden="true">↗</span></a>',
        '    <div class="hero-material-note"><strong>银色金属主题</strong><span>背景取自航空级拉丝铝材质。压印轮廓为视觉表达，不作为事件证据。</span></div>',
        "  </div>",
        '  <aside class="hero-visual-note hero-enter delay-one"><strong>VISUAL RECONSTRUCTION</strong><span>BRUSHED ALUMINUM SURFACE</span><span>NON-EVIDENTIARY</span></aside>',
        '  <dl class="hero-metrics hero-enter delay-one">',
        f"      <div><dt>来源记录</dt><dd>{len(news)}</dd></div>",
        f"      <div><dt>事件档案</dt><dd>{len(events)}</dd></div>",
        f"      <div><dt>最后同步</dt><dd class=\"metric-time\">{_html(_display_datetime(last_sync))}</dd></div>",
        "    </dl>",
        "</section>",
        '<div class="source-strip" aria-label="数据来源和更新时间">',
        "  <span>数据来源</span>",
        f"  <p>{_html(' · '.join(str(getattr(source, 'name', '')) for source in sources[:5]))}</p>",
        f'  <time datetime="{_html_attr(last_sync)}">最后同步 {_html(_display_datetime(last_sync))}</time>',
        "</div>",
        _render_recent_news(news[:5]),
        _render_map_overview(country_counts, missing_location_count, len(events)),
        _render_distribution(category_counts, len(news)),
        _render_evidence_empty_state(news[:3]),
        _render_method(sources, today),
    ]
    return "\n".join(lines) + "\n"


def _render_latest_event(event: dict[str, object] | None) -> str:
    if event is None:
        return """
  <article class="latest-observation hero-enter delay-one">
    <div class="observation-head"><div><span class="section-index">RECENT / EMPTY</span><h2>最近事件</h2></div><span class="verification">暂无记录</span></div>
    <div class="event-empty"><p>事件档案尚未建立。</p></div>
    <a class="text-link" href="events/index.html">查看事件档案 <span aria-hidden="true">↗</span></a>
  </article>""".rstrip()

    name = _html(_text(event.get("event_name")) or "未命名事件")
    date_value = _html(_date_prefix(event.get("date_start")) or "日期未知")
    place = "，".join(
        filter(None, (_text(event.get("location")), _text(event.get("country"))))
    ) or "地点未记录"
    description = _html(_text(event.get("description")) or "暂无公开描述。")
    status = _html(_event_status_label(_text(event.get("status"))))
    credibility = int(event.get("credibility") or 1)
    anchor = _anchor_slug(_text(event.get("event_name")))
    return f"""
  <article class="latest-observation hero-enter delay-one">
    <div class="observation-head">
      <div><span class="section-index">RECENT / EVENT</span><h2>最近事件档案</h2></div>
      <span class="verification">{status}</span>
    </div>
    <div class="observation-plot" aria-label="事件档案定位状态图">
      <div class="plot-grid"></div><span class="plot-ring plot-ring-one"></span><span class="plot-ring plot-ring-two"></span><span class="plot-object"></span>
      <span class="plot-label">ARCHIVE RECORD</span>
      <div class="plot-readout"><span>DATE {date_value}</span><span>LOC {_html(place)}</span></div>
    </div>
    <div class="observation-meta">
      <div><span>事件</span><strong>{name}</strong></div>
      <div><span>地点</span><strong>{_html(place)}</strong></div>
      <div><span>来源评级</span><strong>{credibility} / 5</strong></div>
    </div>
    <p class="observation-note">{description}</p>
    <a class="text-link" href="events/index.html#{anchor}">打开事件档案 <span aria-hidden="true">↗</span></a>
  </article>""".rstrip()


def _render_recent_news(rows: list[dict[str, object]]) -> str:
    lines = [
        '<section class="archive-section timeline-section" id="latest" data-reveal>',
        '  <div class="section-heading">',
        '    <span class="section-number">01</span>',
        "    <h2>最新档案记录</h2>",
        "    <p>按来源发布时间排序。事实状态、来源等级与分析状态分别标注，不把报道等同于结论。</p>",
        "  </div>",
        '  <div class="record-list">',
    ]
    if not rows:
        lines.append('<div class="empty-row">暂无已发布的来源记录。</div>')
    for index, row in enumerate(rows, start=1):
        raw_title = _text(row.get("title")) or _text(row.get("original_title")) or "未命名记录"
        title = _html(raw_title)
        title_attr = _html_attr(raw_title)
        date_value = _html(_date_prefix(row.get("publish_date")) or "日期未知")
        source = _html(_text(row.get("source")) or "未知来源")
        category = _html(_category_label(_text(row.get("category")) or "other"))
        fact_status = _html(_fact_status_label(_text(row.get("fact_status"))))
        summary = _html(_text(row.get("summary")) or _analysis_unavailable_message(row))
        record_url = f"news/{int(row['id'])}.html"
        lines.extend(
            [
                f'    <article class="record-row"><time>{date_value}</time>',
                '      <span class="record-node" aria-hidden="true"><i></i></span>',
                f'      <div class="record-source"><strong>{source}</strong><span>REC-{int(row["id"]):04d}</span></div>',
                f'      <div class="record-copy"><h3><a href="{record_url}">{title}</a></h3><p>{summary}</p></div>',
                f'      <div class="record-type"><span>{category}</span><small>{fact_status}</small></div>',
                f'      <a class="record-open" href="{record_url}" aria-label="打开 {title_attr}">↗</a>',
                f'      <span class="record-order">{index:02d}</span></article>',
            ]
        )
    lines.extend(
        [
            "  </div>",
            '  <a class="section-link" href="news/index.html">查看全部来源记录 <span aria-hidden="true">↗</span></a>',
            "</section>",
        ]
    )
    return "\n".join(lines)


def _render_map_overview(
    country_counts: Counter[str],
    missing_location_count: int,
    event_count: int,
) -> str:
    positions = {
        "USA": (-98.0, 39.0),
        "United States": (-98.0, 39.0),
        "China": (104.0, 35.0),
        "Japan": (138.0, 36.0),
        "France": (2.2, 46.0),
        "UK": (-3.0, 55.0),
        "Chile": (-71.0, -33.0),
        "Brazil": (-52.0, -10.0),
        "Australia": (134.0, -25.0),
    }
    markers: list[str] = []
    for country, count in country_counts.most_common(8):
        position = positions.get(country)
        if not position:
            continue
        longitude, latitude = position
        x = (longitude + 180.0) / 360.0 * 100.0
        y = (90.0 - latitude) / 180.0 * 100.0
        markers.append(
            f'<a class="map-point" href="events/index.html" style="left:{x:.2f}%;top:{y:.2f}%" '
            f'aria-label="{_html_attr(country)}，{count} 条事件"><i></i><span>{count}</span>'
            f"<em>{_html(country)}</em></a>"
        )
    if not markers:
        markers.append(
            '<span class="map-point map-point-empty" style="left:50%;top:50%"><i></i><span>0</span><em>暂无位置</em></span>'
        )
    known_count = sum(country_counts.values())
    return "\n".join(
        [
            '<section class="archive-section map-section" id="event-map" data-reveal>',
            '  <div class="section-heading">',
            '    <span class="section-number">02</span><h2>事件地图概览</h2>',
            "    <p>仅依据事件表中的国家或地区字段定位；不推断缺失坐标，不把区域级位置显示为精确地点。</p>",
            "  </div>",
            '  <div class="map-layout">',
            '    <div class="map-canvas" role="img" aria-label="事件国家或地区分布概览">',
            '      <div class="map-graticule"></div>',
            '      <div class="map-plot"><img class="world-map" src="assets/world-map.svg" alt="" aria-hidden="true">',
            f"        {''.join(markers)}",
            "      </div>",
            '      <div class="map-scale"><span>区域级定位</span><i></i><span>非精确坐标</span></div>',
            "    </div>",
            '    <aside class="map-aside">',
            f'      <div class="map-stat"><span>事件总数</span><strong>{event_count}</strong><small>全部事件档案</small></div>',
            f'      <div class="map-stat"><span>可定位国家</span><strong>{known_count}</strong><small>具有国家字段</small></div>',
            '      <div class="map-register"><span>位置完整度</span>',
            f"        <div><i></i><span>国家级位置</span><b>{known_count}</b></div>",
            f'        <div><i class="muted"></i><span>位置未记录</span><b>{missing_location_count}</b></div>',
            "      </div>",
            '      <a class="button button-primary map-button" href="events/index.html">查看事件档案 <span aria-hidden="true">↗</span></a>',
            "    </aside>",
            "  </div>",
            "</section>",
        ]
    )


def _render_distribution(category_counts: Counter[str], total: int) -> str:
    lines = [
        '<section class="archive-section distribution-section" data-reveal>',
        '  <div class="distribution-intro"><span class="section-number">03</span>',
        "    <h2>记录类型分布</h2>",
        "    <p>分类来自来源记录的受控字段，用于检索与比较；它描述材料性质，不描述物体性质。</p>",
        f'    <div class="distribution-total"><strong>{total}</strong><span>条来源记录<br>{len(category_counts)} 个信息类别</span></div>',
        "  </div>",
        '  <div class="distribution-chart">',
    ]
    for index, (category, count) in enumerate(category_counts.most_common(), start=1):
        percent = (count / total * 100) if total else 0
        lines.append(
            f'    <div class="category-row"><span>{index:02d}</span>'
            f"<strong>{_html(_category_label(category))}</strong>"
            f'<div class="bar-track"><i style="width:{percent:.1f}%"></i></div>'
            f"<b>{count}</b><span>{percent:.1f}%</span></div>"
        )
    if not category_counts:
        lines.append('<div class="empty-row">暂无可统计的记录。</div>')
    lines.extend(
        [
            '    <p class="chart-note">统计范围：当前公开来源记录；分类可能随人工复核而调整。</p>',
            "  </div>",
            "</section>",
        ]
    )
    return "\n".join(lines)


def _render_evidence_empty_state(rows: list[dict[str, object]]) -> str:
    links = []
    for row in rows:
        title = _html(_text(row.get("title")) or _text(row.get("original_title")) or "未命名记录")
        links.append(
            f'<a href="news/{int(row["id"])}.html"><span>来源材料</span><strong>{title}</strong>'
            f'<small>{_html(_text(row.get("source")) or "未知来源")} · '
            f'{_html(_date_prefix(row.get("publish_date")) or "日期未知")}</small></a>'
        )
    source_links = "".join(links) if links else '<p class="empty-row">暂无来源材料。</p>'
    return "\n".join(
        [
            '<section class="archive-section evidence-section" data-reveal>',
            '  <div class="section-heading">',
            '    <span class="section-number">04</span><h2>最近新增的图片或视频证据</h2>',
            "    <p>媒体附件只有在来源许可、原始文件与元数据可追溯时才进入公开证据区。</p>",
            "  </div>",
            '  <div class="evidence-empty">',
            '    <div class="evidence-scope"><span class="scope-code">MEDIA / 000</span><strong>当前无可公开的媒体附件</strong><p>现有数据模型保存来源链接与分析元数据，不复制第三方图片或视频。这里不会用截图或生成图像填充空位。</p></div>',
            '    <div class="source-materials"><div class="source-materials-head"><span>最近来源材料</span><span>替代入口</span></div>',
            f"      {source_links}",
            "    </div>",
            "  </div>",
            "</section>",
        ]
    )


def _render_method(sources: list[object], today: str) -> str:
    source_rows = []
    for source in sources:
        name = _html(str(getattr(source, "name", "") or "未命名来源"))
        url = _html_attr(str(getattr(source, "homepage_url", "") or "#"))
        source_type = _source_type_label(str(getattr(source, "source_type", "")))
        success = str(getattr(source, "last_success_at", "") or "")
        error = str(getattr(source, "last_error", "") or "")
        status = "需关注" if error else ("已同步" if success else "待同步")
        source_rows.append(
            f'<div><a href="{url}" rel="noopener noreferrer">{name}</a>'
            f"<span>{_html(source_type)}</span><time>{_html(_display_datetime(success) if success else '尚无成功记录')}</time>"
            f'<b class="source-state {"warning" if error else ""}">{status}</b></div>'
        )
    return "\n".join(
        [
            '<section class="archive-section method-section" id="method" data-reveal>',
            '  <div class="method-title"><span class="section-number">05</span><h2>数据来源与方法</h2>',
            "    <p>公开不等于未经整理。每条记录经过来源登记、字段标准化、事实状态标注与版本化发布。</p></div>",
            '  <div class="method-flow">',
            "    <div><span>01</span><strong>采集</strong><p>从登记来源获取标题、链接、发布时间与公开正文。</p></div><i aria-hidden=\"true\"></i>",
            "    <div><span>02</span><strong>标准化</strong><p>统一时间、类别、事实状态与来源等级，保留原始链接。</p></div><i aria-hidden=\"true\"></i>",
            "    <div><span>03</span><strong>分析</strong><p>仅在正文可提取时生成摘要，并保留模型、置信度与风险标记。</p></div><i aria-hidden=\"true\"></i>",
            "    <div><span>04</span><strong>发布</strong><p>生成不含第三方正文的静态档案，并保留稳定记录编号。</p></div>",
            "  </div>",
            '  <div class="source-register"><div class="source-register-head"><span>来源登记</span><span>类型</span><span>最后成功</span><span>状态</span></div>',
            f"    {''.join(source_rows)}",
            "  </div>",
            f'  <p class="method-note">页面生成日期：{_html(today)} · 来源状态反映数据库中最近一次采集结果。</p>',
            "</section>",
        ]
    )


def _display_datetime(value: str) -> str:
    if not value:
        return "未知"
    normalized = value.replace("T", " ").replace("Z", " UTC")
    return normalized[:16] + (" UTC" if value.endswith("Z") and "UTC" not in normalized[:16] else "")


def _event_status_label(value: str) -> str:
    return {
        "official_record": "官方记录",
        "unverified": "待核验",
        "verified": "已核验",
        "disputed": "存在争议",
    }.get(value, value or "状态未知")


def _fact_status_label(value: str) -> str:
    return {
        "official_record": "官方记录",
        "source_reported": "来源陈述",
        "opinion": "观点",
        "disputed": "存在争议",
        "unverified": "待核验",
    }.get(value, value or "状态未知")


def _source_type_label(value: str) -> str:
    raw_value = value.rsplit(".", 1)[-1].lower()
    return {"rss": "RSS", "api": "API", "web_page": "网页"}.get(raw_value, raw_value)


def _html(value: str) -> str:
    return html.escape(value, quote=False)


def _html_attr(value: str) -> str:
    return html.escape(value, quote=True)


def _render_news_index(
    rows: list[dict[str, object]],
    news_entities: dict[int, list[dict[str, object]]],
) -> str:
    lines = [
        "---",
        'title: "UAP新闻"',
        "layout: default",
        "---",
        "",
        "[搜索新闻](../search.html) · [标签总览](../tags.html)",
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
        lines.extend(
            _render_news_cards(
                category_rows,
                link_prefix="",
                tag_prefix="../",
                news_entities=news_entities,
            )
        )
    return "\n".join(lines)


def _render_search_index(
    rows: list[dict[str, object]],
    news_entities: dict[int, list[dict[str, object]]],
) -> list[dict[str, object]]:
    """Return a small public index containing metadata, never article bodies."""
    return [
        {
            "id": int(row["id"]),
            "title": _text(row.get("title")) or _text(row.get("original_title")),
            "summary": _text(row.get("summary")),
            "source": _text(row.get("source")),
            "publish_date": _date_prefix(row.get("publish_date")),
            "category": _text(row.get("category")) or "other",
            "tags": _entity_names(news_entities.get(int(row["id"]), [])),
            "fact_status": _text(row.get("fact_status")),
            "credibility": row.get("credibility"),
            "url": f"news/{_news_url(row)}",
        }
        for row in rows
    ]


def _render_search_page() -> str:
    return """---
title: "搜索UAP新闻"
layout: default
---

<input id="uap-search" type="search" placeholder="搜索标题、摘要、来源、分类或标签" style="width:100%;max-width:42rem;padding:.6rem" />
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
        [item.title, item.summary, item.source, item.category, ...(item.tags || [])].join(' ').toLowerCase().includes(query));
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
    tag_prefix: str,
    news_entities: dict[int, list[dict[str, object]]],
) -> list[str]:
    lines: list[str] = []
    for row in rows:
        filename = _news_url(row)
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
        lines.extend(_render_entity_tags(news_entities.get(int(row["id"]), []), tag_prefix))
        lines.append("")
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
        f"发布时间：{_date_prefix(row.get('publish_date')) or '未知'}",
        f"可信度：{_stars(row.get('credibility'))}",
        f"事实状态：`{_escape_text(_text(row.get('fact_status')) or 'unknown')}`",
            f"分析状态：`{_escape_text(_text(row.get('processing_status')) or 'pending')}`",
            f"正文提取状态：`{_escape_text(_text(row.get('extraction_status')) or 'pending')}`",
        "",
        "## 实体标签",
        "",
    ]
    lines.extend(_render_entity_tags(entities, "../"))
    if not entities:
        lines.append("- 暂无单位、人物或事件标签。")
    lines.extend(("", "## AI摘要", "", _escape_text(_ai_summary_or_status_message(row)), "", "## 关键事实", ""))
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
    lines.extend(
        (
            "",
            "## 采集说明",
            "",
            _escape_text(_extraction_status_message(row)),
            "",
            "## 原始来源",
            "",
            f"来源：{_escape_text(_text(row.get('source')) or '未知来源')}",
            "",
            f"[打开原文]({_text(row.get('source_url'))})",
            "",
            "本站不转载抓取的文章正文。",
            "",
        )
    )
    return "\n".join(lines)


def _extraction_status_message(row: dict[str, object]) -> str:
    status = _text(row.get("extraction_status")) or "pending"
    if status == "completed":
        return "正文已成功提取。"
    if status == "failed":
        error = _text(row.get("extraction_error")) or ""
        if "403" in error:
            return "来源服务器拒绝自动抓取（HTTP 403）；请通过下方原始来源链接查看内容。"
        return "正文提取失败；请通过下方原始来源链接查看内容。"
    return "正文尚未提取完成。"


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
        event_name = _text(event.get("event_name")) or "未命名事件"
        lines.extend((f'<a id="{_anchor_slug(event_name)}"></a>', f"## {_escape_text(event_name)}", "", f"时间：{period}"))
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


def _render_persons_index(
    persons: list[dict[str, object]],
    person_news: dict[int, list[dict[str, object]]],
) -> str:
    lines = ["---", 'title: "人物"', "layout: default", "---", ""]
    if not persons:
        return "\n".join(lines + ["暂无已建立的人物实体。", ""])
    for person in persons:
        name = _escape_text(_text(person.get("name")) or "未命名人物")
        organization = _text(person.get("organization"))
        lines.extend((f'<a id="{_anchor_slug(_text(person.get("name")))}"></a>', f"## {name}", ""))
        if organization:
            lines.append(f"机构：{_escape_text(organization)}")
        if person.get("country"):
            lines.append(f"国家/地区：{_escape_text(_text(person.get('country')))}")
        lines.extend(("", _escape_text(_text(person.get("description")) or "暂无描述。"), ""))
        lines.extend(_render_entity_news(person_news.get(int(person["id"]), []), "../news/"))
        lines.append("")
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


def _render_organizations(
    organizations: list[dict[str, object]],
    organization_news: dict[int, list[dict[str, object]]],
) -> str:
    lines = ["---", 'title: "机构"', "layout: default", "---", ""]
    if not organizations:
        return "\n".join(lines + ["暂无已建立的机构实体。", ""])
    for organization in organizations:
        lines.extend(
            (
                f'<a id="{_anchor_slug(_text(organization.get("name")))}"></a>',
                f"## {_escape_text(_text(organization.get('name')) or '未命名机构')}",
                "",
                _escape_text(_text(organization.get("description")) or "暂无描述。"),
                "",
            )
        )
        lines.extend(_render_entity_news(organization_news.get(int(organization["id"]), []), "news/"))
        lines.append("")
    return "\n".join(lines)


def _render_tags_index(
    persons: list[dict[str, object]],
    organizations: list[dict[str, object]],
    person_news: dict[int, list[dict[str, object]]],
    organization_news: dict[int, list[dict[str, object]]],
) -> str:
    lines = [
        "---",
        'title: "标签总览"',
        "layout: default",
        "---",
        "",
        "标签按实体类型整理，并显示已关联的新闻数量。",
        "",
    ]
    if organizations:
        lines.extend(("## 单位", "", "| 标签 | 关联新闻 |", "| --- | ---: |"))
        for organization in organizations:
            name = _text(organization.get("name")) or "未命名单位"
            count = len(organization_news.get(int(organization["id"]), []))
            lines.append(
                f'| [单位：{_escape_table(name)}](organizations.html#{_anchor_slug(name)}) | {count} |'
            )
        lines.append("")
    if persons:
        lines.extend(("## 人物", "", "| 标签 | 关联新闻 |", "| --- | ---: |"))
        for person in persons:
            name = _text(person.get("name")) or "未命名人物"
            count = len(person_news.get(int(person["id"]), []))
            lines.append(
                f'| [人物：{_escape_table(name)}](persons/index.html#{_anchor_slug(name)}) | {count} |'
            )
        lines.append("")
    if not organizations and not persons:
        lines.append("暂无已建立的单位或人物标签。")
    return "\n".join(lines)


def _render_entity_news(news: list[dict[str, object]], link_prefix: str) -> list[str]:
    if not news:
        return ["关联新闻：暂无。"]
    lines = ["关联新闻：", ""]
    for item in news:
        title = _escape_text(_text(item.get("title")) or "未命名新闻")
        date = _date_prefix(item.get("publish_date")) or "日期未知"
        lines.append(f"- [{title}]({link_prefix}{int(item['id'])}.html)（{date}）")
    return lines


def _render_entity_tags(entities: list[dict[str, object]], link_prefix: str) -> list[str]:
    """Render stable, clickable entity labels for reuse across generated pages."""
    tags: list[str] = []
    seen: set[tuple[str, str]] = set()
    for entity in entities:
        entity_type = _text(entity.get("entity_type"))
        name = (
            _text(entity.get("entity_name"))
            or _text(entity.get("event_name"))
            or _text(entity.get("organization_name"))
        )
        if not name or (entity_type, name.lower()) in seen:
            continue
        seen.add((entity_type, name.lower()))
        label = {"person": "人物", "organization": "单位", "event": "事件"}.get(entity_type, "实体")
        href = {
            "person": f"{link_prefix}persons/index.html#{_anchor_slug(name)}",
            "organization": f"{link_prefix}organizations.html#{_anchor_slug(name)}",
            "event": f"{link_prefix}events/index.html#{_anchor_slug(name)}",
        }.get(entity_type, "")
        tag_text = f"{label}：{_escape_text(name)}"
        tags.append(f'<a class="entity-tag" href="{href}">{tag_text}</a>' if href else tag_text)
    if not tags:
        return []
    return ["<div class=\"entity-tags\">", " ".join(tags), "</div>"]


def _entity_names(entities: list[dict[str, object]]) -> list[str]:
    names: list[str] = []
    for entity in entities:
        name = (
            _text(entity.get("entity_name"))
            or _text(entity.get("event_name"))
            or _text(entity.get("organization_name"))
        )
        if name and name not in names:
            names.append(name)
    return names


def _anchor_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", value.lower()).strip("-")
    return slug or "entity"


def _news_filename(row: dict[str, object]) -> str:
    """Use an immutable ID so translated titles never change the public URL."""

    return f"{int(row['id'])}.md"


def _news_url(row: dict[str, object]) -> str:
    """Return the URL emitted by the GitHub Pages Jekyll build."""

    return _news_filename(row).removesuffix(".md") + ".html"


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
    target_url = target_filename.removesuffix(".md") + ".html"
    return "\n".join(
        (
            "---",
            'title: "文章链接已更新"',
            "layout: default",
            "---",
            "",
            f'<meta http-equiv="refresh" content="0; url=../news/{target_url}">',
            f"文章链接已更新，请访问 [最新页面](../news/{target_url})。",
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

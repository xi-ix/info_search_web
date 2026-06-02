from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import re
import time
from typing import Iterable
from urllib.parse import quote_plus
from xml.etree import ElementTree

import feedparser
import requests
import json

from .models import FeedItem

ARXIV_RSS_URLS = [
    "https://export.arxiv.org/rss/cs.AI",
    "https://export.arxiv.org/rss/cs.LG",
    "https://export.arxiv.org/rss/cs.CL",
]
ARXIV_API_URL = "https://export.arxiv.org/api/query"
GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
DEFAULT_NEWS_FEEDS = [
    "https://openai.com/news/rss.xml",
    "https://huggingface.co/blog/feed.xml",
    "https://about.fb.com/news/category/product-news/feed/",
    "https://qwenlm.github.io/blog/index.xml",
]
DEFAULT_NEWS_HTML_SOURCES = [
    {
        "name": "Hugging Face Blog",
        "kind": "huggingface_blog",
        "url": "https://huggingface.co/blog",
    },
    {
        "name": "Meta Newsroom",
        "kind": "meta_newsroom",
        "url": "https://about.fb.com/news/",
    },
    {
        "name": "DeepSeek News",
        "kind": "deepseek_news",
        "url": "https://api-docs.deepseek.com/updates/",
    },
]


def collect_items(config: dict | None = None) -> list[FeedItem]:
    items, _, _ = collect_items_with_status(config)
    return items


def collect_items_with_status(config: dict | None = None) -> tuple[list[FeedItem], dict[str, str], float]:
    config = config or {}
    total_timeout = float(config.get("refresh_timeout_seconds", 8))
    started_at = time.monotonic()
    deadline = started_at + max(total_timeout, 0.1)

    papers, paper_status = fetch_arxiv_papers_with_status(config, deadline)
    projects, project_status = fetch_github_projects_with_status(config, deadline)
    news, news_status = fetch_model_news_with_status(config, deadline)

    items: list[FeedItem] = []
    items.extend(papers)
    items.extend(projects)
    items.extend(news)
    maybe_translate_items(items, config, deadline)
    status = {
        "papers": paper_status,
        "projects": project_status,
        "news": news_status,
    }
    return items, status, time.monotonic() - started_at


def fetch_arxiv_papers(config: dict) -> list[FeedItem]:
    deadline = time.monotonic() + max(float(config.get("refresh_timeout_seconds", 8)), 0.1)
    papers, _ = fetch_arxiv_papers_with_status(config, deadline)
    return papers


def fetch_arxiv_papers_with_status(config: dict, deadline: float) -> tuple[list[FeedItem], str]:
    arxiv_config = config.get("arxiv", {})
    keywords = arxiv_config.get(
        "keywords",
        ["llm", "large language model", "agent", "rag", "multimodal", "reasoning"],
    )
    max_items = int(arxiv_config.get("max_items", 30))
    timeout = float(arxiv_config.get("timeout_seconds", 10))
    min_results = int(arxiv_config.get("min_results", 8))
    api_max_results = int(arxiv_config.get("api_max_results", 20))
    proxy = arxiv_config.get("proxy", "").strip()
    session = build_requests_session(proxy)
    headers = {"User-Agent": "paper-search/0.1 (+https://arxiv.org)"}

    if remaining_seconds(deadline) <= 0:
        return get_demo_papers(), "fallback-timeout|Refresh time budget exhausted before arXiv"

    last_error = ""
    try:
        xml_documents: list[str] = []
        for url in arxiv_config.get("feeds", ARXIV_RSS_URLS):
            response = session.get(url, timeout=bounded_timeout(timeout, deadline), headers=headers)
            response.raise_for_status()
            xml_documents.append(response.text)
        papers = parse_arxiv_rss_documents(xml_documents, keywords, max_items, min_results)
        if papers:
            return papers, f"live-rss-{len(papers)}"
        last_error = "RSS returned no parseable items"
    except Exception as exc:
        last_error = f"RSS error: {exc}"

    try:
        papers = fetch_arxiv_api_papers(
            session, keywords, max_items, api_max_results, timeout, headers, deadline
        )
        if papers:
            return papers, f"live-api-{len(papers)}"
        return get_demo_papers(), f"fallback-empty|{last_error}; API returned no items"
    except Exception as exc:
        detail = f"{last_error}; API error: {exc}" if last_error else f"API error: {exc}"
        return get_demo_papers(), f"fallback-error|{detail}"


def parse_arxiv_rss_documents(
    xml_documents: list[str], keywords: list[str], max_items: int, min_results: int
) -> list[FeedItem]:
    normalized_keywords = [keyword.casefold() for keyword in keywords]
    matched_items: list[FeedItem] = []
    recent_items: list[FeedItem] = []
    seen_links: set[str] = set()

    for xml_text in xml_documents:
        root = ElementTree.fromstring(xml_text)
        channel = find_channel(root)
        if channel is None:
            continue

        for node in iter_local_name(channel, "item"):
            title = collapse_whitespace(get_xml_text(node, "title"))
            summary = clean_arxiv_summary(get_xml_text(node, "description"))
            link = normalize_arxiv_link(get_xml_text(node, "link"), title)
            if not title or link in seen_links:
                continue
            seen_links.add(link)

            published_at = parse_rss_datetime(get_xml_text(node, "pubDate"))
            item = FeedItem(
                title=title,
                summary=summary,
                link=link,
                source="arXiv",
                category="papers",
                published_at=published_at,
                tags=build_tags(title, summary, normalized_keywords, fallback_tag="arxiv"),
                score=0.0,
            )
            recent_items.append(item)

            haystack = f"{title}\n{summary}".casefold()
            if normalized_keywords and any(keyword in haystack for keyword in normalized_keywords):
                matched_items.append(item)

    return merge_recent_and_matched(matched_items, recent_items, max_items, min_results)


def fetch_arxiv_api_papers(
    session: requests.Session,
    keywords: list[str],
    max_items: int,
    api_max_results: int,
    timeout: float,
    headers: dict[str, str],
    deadline: float,
) -> list[FeedItem]:
    query = build_arxiv_api_query(keywords)
    response = session.get(
        ARXIV_API_URL,
        params={
            "search_query": query,
            "start": 0,
            "max_results": api_max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        },
        timeout=bounded_timeout(timeout, deadline),
        headers=headers,
    )
    response.raise_for_status()
    root = ElementTree.fromstring(response.text)
    normalized_keywords = [keyword.casefold() for keyword in keywords]

    items: list[FeedItem] = []
    for entry in iter_local_name(root, "entry"):
        title = collapse_whitespace(get_xml_text(entry, "title"))
        summary = clean_arxiv_summary(get_xml_text(entry, "summary"))
        link = get_arxiv_entry_link(entry)
        published_at = parse_rss_datetime(get_xml_text(entry, "published"))
        items.append(
            FeedItem(
                title=title,
                summary=summary,
                link=link,
                source="arXiv",
                category="papers",
                published_at=published_at,
                tags=build_tags(title, summary, normalized_keywords, fallback_tag="arxiv"),
                score=0.0,
            )
        )
        if len(items) >= max_items:
            break
    return items


def fetch_github_projects_with_status(config: dict, deadline: float) -> tuple[list[FeedItem], str]:
    github_config = config.get("github", {})
    keywords = github_config.get("keywords", ["llm", "agent", "rag", "multimodal", "reasoning"])
    max_items = int(github_config.get("max_items", 20))
    min_stars = int(github_config.get("min_stars", 100))
    sort = github_config.get("sort", "updated")
    order = github_config.get("order", "desc")
    timeout = float(github_config.get("timeout_seconds", 10))
    proxy = github_config.get("proxy", config.get("arxiv", {}).get("proxy", "")).strip()
    github_token = github_config.get("token", "").strip()
    session = build_requests_session(proxy)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "paper-search/0.1",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    if remaining_seconds(deadline) <= 0:
        return get_demo_projects(), "fallback-timeout|Refresh time budget exhausted before GitHub"

    query = build_github_query(keywords, min_stars)
    try:
        response = session.get(
            GITHUB_SEARCH_URL,
            params={
                "q": query,
                "sort": sort,
                "order": order,
                "per_page": min(max_items, 30),
            },
            timeout=bounded_timeout(timeout, deadline),
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
        repositories = payload.get("items", [])
        items = [
            FeedItem(
                title=repo.get("full_name", "Unknown Repo"),
                summary=collapse_whitespace(repo.get("description") or "No description provided."),
                link=repo.get("html_url", "https://github.com"),
                source="GitHub",
                category="projects",
                published_at=parse_rss_datetime(repo.get("pushed_at", "")),
                tags=extract_repo_tags(repo, keywords),
                score=float(repo.get("stargazers_count", 0)),
            )
            for repo in repositories
        ]
        if items:
            visible = items[:max_items]
            return visible, f"live-github-{len(visible)}"
        return get_demo_projects(), "fallback-empty|GitHub returned no repositories"
    except Exception as exc:
        return get_demo_projects(), f"fallback-error|GitHub error: {exc}"


def fetch_model_news_with_status(config: dict, deadline: float) -> tuple[list[FeedItem], str]:
    news_config = config.get("news", {})
    keywords = news_config.get(
        "keywords",
        ["model", "llm", "agent", "reasoning", "multimodal", "release", "api"],
    )
    max_items = int(news_config.get("max_items", 20))
    recent_days = int(news_config.get("recent_days", config.get("recent_days", 7)))
    timeout = float(news_config.get("timeout_seconds", 10))
    proxy = news_config.get("proxy", config.get("arxiv", {}).get("proxy", "")).strip()
    session = build_requests_session(proxy)
    headers = {"User-Agent": "paper-search/0.1"}

    if remaining_seconds(deadline) <= 0:
        return get_demo_news(), "fallback-timeout|Refresh time budget exhausted before news"

    errors: list[str] = []
    xml_documents: list[tuple[str, str]] = []
    html_items: list[FeedItem] = []

    for url in news_config.get("feeds", DEFAULT_NEWS_FEEDS):
        if remaining_seconds(deadline) <= 0:
            errors.append("News feeds skipped due to refresh time budget exhaustion")
            break
        try:
            response = session.get(url, timeout=bounded_timeout(timeout, deadline), headers=headers)
            response.raise_for_status()
            xml_documents.append((url, response.text))
        except Exception as exc:
            errors.append(f"{derive_source_name(url)}: {exc}")

    for source in news_config.get("html_sources", DEFAULT_NEWS_HTML_SOURCES):
        if remaining_seconds(deadline) <= 0:
            errors.append("HTML news sources skipped due to refresh time budget exhaustion")
            break
        try:
            response = session.get(source["url"], timeout=bounded_timeout(timeout, deadline), headers=headers)
            response.raise_for_status()
            html_items.extend(parse_news_html_source(source, response.text, keywords, recent_days))
        except Exception as exc:
            errors.append(f"{source.get('name', 'HTML source')}: {exc}")

    rss_items = parse_news_documents(xml_documents, keywords, max_items, recent_days)
    cutoff = datetime.now() - timedelta(days=recent_days) if recent_days > 0 else None
    filtered_html_items = [item for item in html_items if cutoff is None or item.published_at >= cutoff]
    items = merge_recent_and_matched([], rss_items + filtered_html_items, max_items, min_results=0)

    if items:
        if errors:
            return items, f"live-news-{len(items)}|Partial feed errors: {'; '.join(errors[:3])}"
        return items, f"live-news-{len(items)}"

    detail = "; ".join(errors[:3]) if errors else "News feeds returned no parseable items"
    return get_demo_news(), f"fallback-empty|{detail}"


def parse_news_documents(
    xml_documents: list[tuple[str, str]], keywords: list[str], max_items: int, recent_days: int
) -> list[FeedItem]:
    normalized_keywords = [keyword.casefold() for keyword in keywords]
    matched_items: list[FeedItem] = []
    recent_items: list[FeedItem] = []
    seen_links: set[str] = set()
    cutoff = datetime.now() - timedelta(days=recent_days) if recent_days > 0 else None

    for source_url, xml_text in xml_documents:
        parsed = feedparser.parse(xml_text)
        feed_title = collapse_whitespace(parsed.feed.get("title", "")) or derive_source_name(source_url)

        for entry in parsed.entries:
            title = collapse_whitespace(entry.get("title", ""))
            summary = clean_news_summary(
                entry.get("summary", "")
                or entry.get("description", "")
                or entry.get("content", [{}])[0].get("value", "")
            )
            link = (entry.get("link", "") or "").strip()
            if not title or not link or link in seen_links:
                continue
            seen_links.add(link)

            published_at = parse_rss_datetime(entry.get("published", "") or entry.get("updated", ""))
            if cutoff is not None and published_at < cutoff:
                continue

            item = FeedItem(
                title=title,
                summary=summary,
                link=link,
                source=feed_title,
                category="news",
                published_at=published_at,
                tags=build_tags(title, summary, normalized_keywords, fallback_tag="news"),
                score=0.0,
            )
            recent_items.append(item)

            haystack = f"{title}\n{summary}".casefold()
            if normalized_keywords and any(keyword in haystack for keyword in normalized_keywords):
                matched_items.append(item)

    return merge_recent_and_matched(matched_items, recent_items, max_items, min_results=6)


def parse_news_html_source(source: dict, html: str, keywords: list[str], recent_days: int) -> list[FeedItem]:
    kind = source.get("kind", "")
    if kind == "huggingface_blog":
        return parse_huggingface_blog_listing(source, html, keywords, recent_days)
    if kind == "meta_newsroom":
        return parse_meta_newsroom_listing(source, html, keywords, recent_days)
    if kind == "deepseek_news":
        return parse_deepseek_news_listing(source, html, keywords, recent_days)
    return []


def parse_huggingface_blog_listing(source: dict, html: str, keywords: list[str], recent_days: int) -> list[FeedItem]:
    normalized_keywords = [keyword.casefold() for keyword in keywords]
    slug_matches = re.findall(r'/blog/([a-zA-Z0-9._\-\/]+)"', html)
    title_matches = re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.DOTALL)
    items: list[FeedItem] = []
    seen_links: set[str] = set()

    for index, slug in enumerate(slug_matches):
        if slug in {"feed.xml", "zh"} or slug.startswith("assets/"):
            continue
        link = absolute_url(source["url"], f"/blog/{slug}")
        if link in seen_links:
            continue
        seen_links.add(link)
        title = clean_html_fragment(title_matches[index]) if index < len(title_matches) else slug.rsplit("/", 1)[-1]
        items.append(
            FeedItem(
                title=title,
                summary="Hugging Face 官方博客更新。",
                link=link,
                source=source.get("name", "Hugging Face Blog"),
                category="news",
                published_at=datetime.now(),
                tags=build_tags(title, title, normalized_keywords, fallback_tag="news"),
                score=0.0,
            )
        )
        if len(items) >= 12:
            break
    return items


def parse_meta_newsroom_listing(source: dict, html: str, keywords: list[str], recent_days: int) -> list[FeedItem]:
    normalized_keywords = [keyword.casefold() for keyword in keywords]
    pattern = re.compile(
        r'href="(?P<href>https://about\.fb\.com/news/(?!category/|tag/|page/)[^"#]+/|/news/(?!category/|tag/|page/)[^"#]+/)"[^>]*>.*?<h3[^>]*>(?P<title>.*?)</h3>.*?<time[^>]*datetime="(?P<iso_date>[^"]+)"[^>]*>(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})</time>',
        re.DOTALL,
    )
    return build_html_items_from_matches(
        source,
        pattern,
        html,
        normalized_keywords,
        recent_days,
        default_summary="Meta 官方产品与 AI 新闻。",
    )


def parse_deepseek_news_listing(source: dict, html: str, keywords: list[str], recent_days: int) -> list[FeedItem]:
    normalized_keywords = [keyword.casefold() for keyword in keywords]
    href_matches = re.findall(r'href="(/news/news[^"]+)"', html)
    title_matches = re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.DOTALL)
    date_matches = re.findall(r"Date:\s*(\d{4}-\d{2}-\d{2})", html)
    items: list[FeedItem] = []
    seen_links: set[str] = set()

    for index, href in enumerate(href_matches):
        link = absolute_url(source["url"], unescape(href))
        if link in seen_links:
            continue
        seen_links.add(link)
        title = clean_html_fragment(title_matches[index]) if index < len(title_matches) else link.rsplit("/", 1)[-1]
        raw_date = date_matches[index] if index < len(date_matches) else ""
        published_at = parse_rss_datetime(raw_date) if raw_date else datetime.now()
        if not within_recent_days(published_at, recent_days):
            continue
        items.append(
            FeedItem(
                title=title,
                summary="DeepSeek 官方发布与 API / 模型动态。",
                link=link,
                source=source.get("name", "DeepSeek News"),
                category="news",
                published_at=published_at,
                tags=build_tags(title, title, normalized_keywords, fallback_tag="deepseek"),
                score=0.0,
            )
        )
    return items


def build_html_items_from_matches(
    source: dict,
    pattern: re.Pattern[str],
    html: str,
    normalized_keywords: list[str],
    recent_days: int,
    default_summary: str = "",
) -> list[FeedItem]:
    items: list[FeedItem] = []
    seen_links: set[str] = set()

    for match in pattern.finditer(html):
        link = absolute_url(source["url"], unescape(match.group("href")))
        if link in seen_links:
            continue
        seen_links.add(link)
        title = clean_html_fragment(match.group("title"))
        summary_group = match.groupdict().get("summary", "")
        summary = clean_html_fragment(summary_group) if summary_group else default_summary
        raw_date = match.groupdict().get("iso_date", "") or match.groupdict().get("date", "")
        published_at = parse_rss_datetime(raw_date) if raw_date else datetime.now()
        if not title or not within_recent_days(published_at, recent_days):
            continue
        items.append(
            FeedItem(
                title=title,
                summary=summary or "官方动态更新。",
                link=link,
                source=source.get("name", "Model News"),
                category="news",
                published_at=published_at,
                tags=build_tags(title, summary or title, normalized_keywords, fallback_tag="news"),
                score=0.0,
            )
        )
    return items


def merge_recent_and_matched(
    matched_items: list[FeedItem], recent_items: list[FeedItem], max_items: int, min_results: int
) -> list[FeedItem]:
    recent_items.sort(key=lambda item: item.published_at, reverse=True)
    matched_items.sort(key=lambda item: item.published_at, reverse=True)
    if min_results > 0 and len(matched_items) >= min_results:
        return matched_items[:max_items]

    merged: list[FeedItem] = []
    seen_links: set[str] = set()
    for item in matched_items + recent_items:
        if item.link in seen_links:
            continue
        seen_links.add(item.link)
        merged.append(item)
        if len(merged) >= max_items:
            break
    return merged


def build_requests_session(proxy: str) -> requests.Session:
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def get_demo_papers() -> list[FeedItem]:
    now = datetime.now()
    return [
        FeedItem(
            title="CRAG -- Comprehensive RAG Benchmark",
            summary="提出一个覆盖多领域、多时间动态性的 RAG 基准，用来衡量真实问答场景里检索增强系统的可靠性与幻觉问题。",
            link="https://arxiv.org/abs/2406.04744",
            source="arXiv",
            category="papers",
            published_at=now - timedelta(hours=8),
            tags=["rag", "benchmark", "evaluation"],
            score=9.3,
        ),
        FeedItem(
            title="Transformers are SSMs: Generalized Models and Efficient Algorithms Through Structured State Space Duality",
            summary="从结构化状态空间对偶性的角度连接 Transformer 与 SSM，并给出更高效的 Mamba-2 架构与算法。",
            link="https://arxiv.org/abs/2405.21060",
            source="arXiv",
            category="papers",
            published_at=now - timedelta(days=1, hours=3),
            tags=["ssm", "transformer", "mamba"],
            score=8.8,
        ),
    ]


def get_demo_projects() -> list[FeedItem]:
    now = datetime.now()
    return [
        FeedItem(
            title="OpenCompass",
            summary="一个面向大模型评测的开源框架，支持多模型、多数据集和统一评测流程，适合做能力对比与回归验证。",
            link="https://github.com/open-compass/OpenCompass",
            source="GitHub",
            category="projects",
            published_at=now - timedelta(hours=15),
            tags=["evaluation", "benchmark", "agent"],
            score=9.1,
        ),
        FeedItem(
            title="RAGFlow",
            summary="一个面向 RAG 场景的开源项目，提供文档解析、检索与问答工作流，适合快速搭建知识库系统。",
            link="https://github.com/infiniflow/ragflow",
            source="GitHub",
            category="projects",
            published_at=now - timedelta(days=2),
            tags=["rag", "tooling", "observability"],
            score=8.5,
        ),
    ]


def get_demo_news() -> list[FeedItem]:
    now = datetime.now()
    return [
        FeedItem(
            title="OpenAI - Introducing GPT-4.1 in the API",
            summary="OpenAI 发布 GPT-4.1 系列模型，强调代码、指令跟随和长上下文能力，适合跟踪模型能力更新。",
            link="https://openai.com/index/introducing-gpt-4-1-in-the-api/",
            source="OpenAI News",
            category="news",
            published_at=now - timedelta(hours=6),
            tags=["release", "api", "context"],
            score=8.9,
        ),
        FeedItem(
            title="Anthropic - Introducing Claude 3.7 Sonnet",
            summary="Anthropic 介绍 Claude 3.7 Sonnet，重点放在混合推理、编码和真实任务中的综合能力提升。",
            link="https://www.anthropic.com/news/claude-3-7-sonnet",
            source="Anthropic News",
            category="news",
            published_at=now - timedelta(days=1, hours=6),
            tags=["reasoning", "coding", "release"],
            score=8.1,
        ),
    ]


def filter_items_by_days(items: list[FeedItem], days: int) -> list[FeedItem]:
    if days <= 0:
        return items
    cutoff = datetime.now() - timedelta(days=days)
    return [item for item in items if item.published_at >= cutoff]


def group_items(items: list[FeedItem], limits: dict[str, int] | None = None) -> dict[str, list[FeedItem]]:
    grouped = {"papers": [], "projects": [], "news": []}
    for item in sorted(items, key=lambda value: value.published_at, reverse=True):
        grouped.setdefault(item.category, []).append(item)
    if limits:
        for category, limit in limits.items():
            if category in grouped and limit > 0:
                grouped[category] = grouped[category][:limit]
    return grouped


def parse_rss_datetime(value: str) -> datetime:
    if not value:
        return datetime.now()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def normalize_arxiv_link(link: str, title: str) -> str:
    if link:
        return link
    return f"https://arxiv.org/search/?query={quote_plus(title)}&searchtype=all"


def clean_arxiv_summary(summary: str) -> str:
    lines = [line.strip() for line in summary.splitlines() if line.strip()]
    filtered = [line for line in lines if not line.startswith("Authors:") and not line.startswith("Categories:")]
    return collapse_whitespace(" ".join(filtered))


def clean_news_summary(summary: str) -> str:
    text = summary.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    if "<" in text:
        try:
            text_nodes = ElementTree.fromstring(f"<root>{text}</root>").itertext()
        except ElementTree.ParseError:
            text_nodes = [text]
    else:
        text_nodes = [text]
    return collapse_whitespace(unescape(" ".join(text_nodes)))


def get_xml_text(node: ElementTree.Element, tag: str) -> str:
    for child in node:
        if child.tag.rsplit("}", 1)[-1] == tag and child.text:
            return child.text.strip()
    direct = node.find(tag)
    if direct is None or direct.text is None:
        return ""
    return direct.text.strip()


def get_arxiv_entry_link(entry: ElementTree.Element) -> str:
    for child in entry:
        if child.tag.rsplit("}", 1)[-1] != "link":
            continue
        href = child.attrib.get("href", "").strip()
        rel = child.attrib.get("rel", "").strip()
        title = child.attrib.get("title", "").strip().lower()
        if href and (title == "pdf" or rel == "related"):
            continue
        if href:
            return href
    return get_xml_text(entry, "id")


def find_channel(root: ElementTree.Element) -> ElementTree.Element | None:
    if root.tag.rsplit("}", 1)[-1] == "channel":
        return root
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == "channel":
            return node
    return None


def iter_local_name(node: ElementTree.Element, local_name: str):
    for child in node.iter():
        if child.tag.rsplit("}", 1)[-1] == local_name:
            yield child


def derive_source_name(url: str) -> str:
    if "openai.com" in url:
        return "OpenAI News"
    if "anthropic.com" in url:
        return "Anthropic News"
    if "huggingface.co" in url:
        return "Hugging Face Blog"
    if "about.fb.com" in url:
        return "Meta Newsroom"
    if "deepseek.com" in url:
        return "DeepSeek News"
    if "qwen" in url:
        return "Qwen Blog"
    return "Model News"


def build_arxiv_api_query(keywords: list[str]) -> str:
    cleaned = [keyword.strip() for keyword in keywords if keyword.strip()]
    if not cleaned:
        return "cat:cs.AI+OR+cat:cs.LG+OR+cat:cs.CL"
    terms = [f'all:"{keyword}"' for keyword in cleaned]
    keyword_clause = "+OR+".join(terms)
    category_clause = "(cat:cs.AI+OR+cat:cs.LG+OR+cat:cs.CL)"
    return f"({keyword_clause})+AND+{category_clause}"


def build_github_query(keywords: list[str], min_stars: int) -> str:
    cleaned = [keyword.strip() for keyword in keywords if keyword.strip()]
    keyword_clause = " OR ".join(cleaned) if cleaned else "llm OR agent OR rag"
    return f"({keyword_clause}) stars:>={min_stars} fork:false archived:false"


def extract_repo_tags(repo: dict, keywords: list[str]) -> list[str]:
    topics = [topic.lower() for topic in repo.get("topics", [])[:3]]
    if topics:
        return topics
    return build_tags(
        repo.get("full_name", ""),
        repo.get("description", "") or "",
        [keyword.casefold() for keyword in keywords],
        fallback_tag="github",
    )


def collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def clean_html_fragment(value: str) -> str:
    return collapse_whitespace(unescape(re.sub(r"<[^>]+>", " ", value)))


def absolute_url(base_url: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def within_recent_days(published_at: datetime, recent_days: int) -> bool:
    if recent_days <= 0:
        return True
    return published_at >= datetime.now() - timedelta(days=recent_days)


def remaining_seconds(deadline: float) -> float:
    return deadline - time.monotonic()


def bounded_timeout(request_timeout: float, deadline: float) -> float:
    remaining = remaining_seconds(deadline)
    if remaining <= 0:
        raise TimeoutError("Refresh time budget exhausted")
    return max(0.1, min(request_timeout, remaining))


def build_tags(title: str, summary: str, keywords: Iterable[str], fallback_tag: str) -> list[str]:
    haystack = f"{title}\n{summary}".casefold()
    tags: list[str] = []
    for keyword in keywords:
        normalized = keyword.strip().lower()
        if normalized and normalized in haystack and normalized not in tags:
            tags.append(normalized)
        if len(tags) >= 3:
            break
    return tags or [fallback_tag]


def maybe_translate_items(items: list[FeedItem], config: dict, deadline: float) -> None:
    """按配置可选补充中文标题和中文简介。"""
    translate_config = config.get("translation", {})
    if not translate_config.get("enabled", False):
        return
    if remaining_seconds(deadline) <= 0:
        return

    backend = translate_config.get("backend", "").strip().lower()
    if backend == "openai":
        translate_with_openai(items, translate_config, deadline)
    elif backend == "libretranslate":
        translate_with_libretranslate(items, translate_config, deadline)
    elif backend == "mymemory":
        translate_with_mymemory(items, translate_config, deadline)


def translate_with_openai(items: list[FeedItem], translate_config: dict, deadline: float) -> None:
    """使用兼容 OpenAI Chat Completions 的接口做中英双语翻译。"""
    api_key = translate_config.get("api_key", "").strip()
    if not api_key:
        return

    base_url = translate_config.get("base_url", "https://api.openai.com/v1").rstrip("/")
    model = translate_config.get("model", "gpt-4.1-mini")
    timeout = float(translate_config.get("timeout_seconds", 20))
    batch_size = int(translate_config.get("batch_size", 5))

    session = requests.Session()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for start in range(0, len(items), batch_size):
        if remaining_seconds(deadline) <= 0:
            break

        batch = items[start:start + batch_size]
        payload_items = [
            {"index": index, "title": item.title, "summary": item.summary}
            for index, item in enumerate(batch)
        ]
        body = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Translate each English title and summary into concise, natural Simplified Chinese. "
                        "Keep product names, model names, organization names, and repository names accurate. "
                        "Return strict JSON object with key 'items', whose value is an array of "
                        "{index, title_zh, summary_zh}."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload_items, ensure_ascii=False),
                },
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        try:
            response = session.post(
                f"{base_url}/chat/completions",
                json=body,
                timeout=min(timeout, max(0.5, remaining_seconds(deadline))),
                headers=headers,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            for record in parse_translation_response(content):
                idx = int(record.get("index", -1))
                if 0 <= idx < len(batch):
                    batch[idx].title_zh = record.get("title_zh", "").strip()
                    batch[idx].summary_zh = record.get("summary_zh", "").strip()
        except Exception:
            continue


def parse_translation_response(content: str) -> list[dict]:
    """兼容对象/数组两种返回格式。"""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return []

    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        if isinstance(parsed.get("items"), list):
            return parsed["items"]
        if isinstance(parsed.get("translations"), list):
            return parsed["translations"]
    return []


def translate_with_libretranslate(items: list[FeedItem], translate_config: dict, deadline: float) -> None:
    """使用普通机器翻译接口做中英双语翻译，不依赖大模型。"""
    base_url = translate_config.get("base_url", "").rstrip("/")
    api_key = translate_config.get("api_key", "").strip()
    timeout = float(translate_config.get("timeout_seconds", 20))
    source_lang = translate_config.get("source_lang", "en")
    target_lang = translate_config.get("target_lang", "zh")

    if not base_url:
        return

    session = requests.Session()
    endpoint = f"{base_url}/translate"

    for item in items:
        if remaining_seconds(deadline) <= 0:
            break
        item.title_zh = translate_text_with_libretranslate(
            session, endpoint, item.title, api_key, source_lang, target_lang, timeout, deadline
        )
        if remaining_seconds(deadline) <= 0:
            break
        item.summary_zh = translate_text_with_libretranslate(
            session, endpoint, item.summary, api_key, source_lang, target_lang, timeout, deadline
        )


def translate_text_with_libretranslate(
    session: requests.Session,
    endpoint: str,
    text: str,
    api_key: str,
    source_lang: str,
    target_lang: str,
    timeout: float,
    deadline: float,
) -> str:
    """调用普通机器翻译接口翻译单段文本。"""
    if not text.strip():
        return ""

    body = {
        "q": text,
        "source": source_lang,
        "target": target_lang,
        "format": "text",
    }
    if api_key:
        body["api_key"] = api_key

    try:
        response = session.post(
            endpoint,
            json=body,
            timeout=min(timeout, max(0.5, remaining_seconds(deadline))),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload.get("translatedText", "")).strip()
    except Exception:
        return ""


def translate_with_mymemory(items: list[FeedItem], translate_config: dict, deadline: float) -> None:
    """使用 MyMemory 接口做中英双语翻译，不依赖大模型。"""
    base_url = translate_config.get("base_url", "https://api.mymemory.translated.net").rstrip("/")
    timeout = float(translate_config.get("timeout_seconds", 20))
    source_lang = translate_config.get("source_lang", "en")
    target_lang = translate_config.get("target_lang", "zh-CN")
    email = translate_config.get("email", "").strip()

    session = requests.Session()
    endpoint = f"{base_url}/get"

    for item in items:
        if remaining_seconds(deadline) <= 0:
            break
        item.title_zh = translate_text_with_mymemory(
            session, endpoint, item.title, source_lang, target_lang, email, timeout, deadline
        )
        if remaining_seconds(deadline) <= 0:
            break
        item.summary_zh = translate_text_with_mymemory(
            session, endpoint, item.summary, source_lang, target_lang, email, timeout, deadline
        )


def translate_text_with_mymemory(
    session: requests.Session,
    endpoint: str,
    text: str,
    source_lang: str,
    target_lang: str,
    email: str,
    timeout: float,
    deadline: float,
) -> str:
    """调用 MyMemory 翻译单段文本。"""
    if not text.strip():
        return ""

    params = {
        "q": text,
        "langpair": f"{source_lang}|{target_lang}",
    }
    if email:
        params["de"] = email

    try:
        response = session.get(
            endpoint,
            params=params,
            timeout=min(timeout, max(0.5, remaining_seconds(deadline))),
            headers={"User-Agent": "paper-search/0.1"},
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload.get("responseData", {}).get("translatedText", "")).strip()
    except Exception:
        return ""

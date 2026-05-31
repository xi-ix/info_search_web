from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

from .collector import collect_items_with_status, filter_items_by_days, group_items
from .storage import load_favorites, toggle_favorite, toggle_favorite_with_result

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
FAVORITES_PATH = BASE_DIR / "data" / "favorites.json"


def load_config() -> dict:
    """读取本地配置；如果缺失则回退到默认值。"""
    default_config = {
        "refresh_timeout_seconds": 8,
        "recent_days": 7,
        "arxiv": {
            "feeds": [
                "https://export.arxiv.org/rss/cs.AI",
                "https://export.arxiv.org/rss/cs.LG",
                "https://export.arxiv.org/rss/cs.CL",
            ],
            "max_items": 30,
            "min_results": 8,
            "api_max_results": 20,
            "proxy": "",
            "timeout_seconds": 10,
            "keywords": [
                "llm",
                "large language model",
                "agent",
                "rag",
                "multimodal",
                "reasoning",
            ],
        },
        "github": {
            "max_items": 20,
            "min_stars": 200,
            "sort": "updated",
            "order": "desc",
            "timeout_seconds": 10,
            "proxy": "",
            "token": "",
            "keywords": [
                "llm",
                "agent",
                "rag",
                "multimodal",
                "reasoning",
            ],
        },
        "news": {
            "feeds": [
                "https://openai.com/news/rss.xml",
                "https://huggingface.co/blog/feed.xml",
                "https://about.fb.com/news/category/product-news/feed/",
                "https://qwenlm.github.io/blog/index.xml"
            ],
            "html_sources": [
                {
                    "name": "Hugging Face Blog",
                    "kind": "huggingface_blog",
                    "url": "https://huggingface.co/blog"
                },
                {
                    "name": "Meta Newsroom",
                    "kind": "meta_newsroom",
                    "url": "https://about.fb.com/news/"
                },
                {
                    "name": "DeepSeek News",
                    "kind": "deepseek_news",
                    "url": "https://api-docs.deepseek.com/updates/"
                }
            ],
            "max_items": 20,
            "recent_days": 10,
            "timeout_seconds": 4,
            "proxy": "",
            "keywords": [
                "model",
                "llm",
                "agent",
                "reasoning",
                "multimodal",
                "release",
                "api",
            ],
        },
        "translation": {
            "enabled": False,
            "backend": "mymemory",
            "base_url": "https://api.mymemory.translated.net",
            "model": "",
            "api_key": "",
            "timeout_seconds": 20,
            "batch_size": 5,
            "source_lang": "en",
            "target_lang": "zh-CN",
            "email": "",
        },
        "limits": {
            "papers": 10,
            "projects": 10,
            "news": 10,
        }
    }
    if not CONFIG_PATH.exists():
        return default_config

    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        loaded = json.load(file)

    config = default_config | loaded
    config["limits"] = default_config["limits"] | loaded.get("limits", {})
    config["arxiv"] = default_config["arxiv"] | loaded.get("arxiv", {})
    config["github"] = default_config["github"] | loaded.get("github", {})
    config["news"] = default_config["news"] | loaded.get("news", {})
    config["translation"] = default_config["translation"] | loaded.get("translation", {})
    if "html_sources" in loaded.get("news", {}):
        config["news"]["html_sources"] = loaded["news"]["html_sources"]
    return config


def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")

    @app.get("/")
    def index():
        config = load_config()
        recent_days = int(config.get("recent_days", 7))
        items, source_status, refresh_elapsed_seconds = collect_items_with_status(config)
        items = filter_items_by_days(items, recent_days)
        limits = config.get("limits", {})
        grouped = group_items(items, limits)
        favorites = load_favorites(FAVORITES_PATH)
        favorite_links = {item["link"] for item in favorites}
        counts = {key: len(value) for key, value in grouped.items()}
        visible_items = [item for group in grouped.values() for item in group]
        tag_counts = Counter(tag for item in visible_items for tag in item.tags).most_common(6)
        freshest = max(visible_items, key=lambda item: item.published_at) if visible_items else None
        return render_template(
            "index.html",
            grouped=grouped,
            counts=counts,
            tag_counts=tag_counts,
            freshest=freshest,
            favorites=favorites,
            favorite_links=favorite_links,
            limits=limits,
            source_status=source_status,
            paper_source_detail=source_status.get("papers", ""),
            project_source_detail=source_status.get("projects", ""),
            news_source_detail=source_status.get("news", ""),
            recent_days=recent_days,
            refresh_timeout_seconds=float(config.get("refresh_timeout_seconds", 8)),
            refresh_elapsed_seconds=refresh_elapsed_seconds,
            now=datetime.now(),
        )

    @app.post("/favorites")
    def save_favorite():
        """收藏或取消收藏当前条目，并保存到本地文件。"""
        payload = {
            "title": request.form["title"],
            "summary": request.form["summary"],
            "link": request.form["link"],
            "source": request.form["source"],
            "category": request.form["category"],
        }
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            is_favorited, favorites = toggle_favorite_with_result(FAVORITES_PATH, payload)
            return jsonify(
                {
                    "ok": True,
                    "is_favorited": is_favorited,
                    "favorites": favorites,
                }
            )

        toggle_favorite(FAVORITES_PATH, payload)
        anchor = request.form.get("anchor", "").strip() or None
        return redirect(url_for("index", _anchor=anchor))

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)

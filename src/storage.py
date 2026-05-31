from __future__ import annotations

import json
from pathlib import Path


def load_favorites(path: Path) -> list[dict]:
    """读取本地收藏列表。"""
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def toggle_favorite(path: Path, item: dict) -> None:
    """按链接收藏或取消收藏，并写回本地 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    favorites = load_favorites(path)

    exists = any(saved["link"] == item["link"] for saved in favorites)
    if exists:
        favorites = [saved for saved in favorites if saved["link"] != item["link"]]
    else:
        favorites.insert(0, item)

    with path.open("w", encoding="utf-8") as file:
        json.dump(favorites, file, ensure_ascii=False, indent=2)


def toggle_favorite_with_result(path: Path, item: dict) -> tuple[bool, list[dict]]:
    """切换收藏状态，并返回当前是否已收藏和最新收藏列表。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    favorites = load_favorites(path)

    exists = any(saved["link"] == item["link"] for saved in favorites)
    if exists:
        favorites = [saved for saved in favorites if saved["link"] != item["link"]]
        is_favorited = False
    else:
        favorites.insert(0, item)
        is_favorited = True

    with path.open("w", encoding="utf-8") as file:
        json.dump(favorites, file, ensure_ascii=False, indent=2)

    return is_favorited, favorites

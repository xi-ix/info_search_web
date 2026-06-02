from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class FeedItem:
    title: str
    summary: str
    link: str
    source: str
    category: str
    published_at: datetime
    tags: list[str] = field(default_factory=list)
    score: float = 0.0
    title_zh: str = ""
    summary_zh: str = ""

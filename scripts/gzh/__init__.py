"""gzh —— 公众号文章获取插件。

抓取微信公众号文章正文，按期限筛选博主历史文章，导出为本地 Markdown 知识库。
"""

from .config import Config
from .converter import html_to_markdown, sanitize_filename
from .exporter import ExportedArticle, KnowledgeBase, render_markdown
from .fetcher import (
    ArticleMeta,
    FetchError,
    FetchResult,
    diagnose_url,
    fetch_article,
    is_mp_article_url,
    list_all_articles,
    parse_meta,
    search_account,
    verify_auth_key,
)
from .period import PERIOD_LABELS, Period, PeriodError, filter_by_period, parse_period

__version__ = "1.0.0"

__all__ = [
    "ArticleMeta",
    "Config",
    "ExportedArticle",
    "FetchError",
    "FetchResult",
    "KnowledgeBase",
    "PERIOD_LABELS",
    "Period",
    "PeriodError",
    "diagnose_url",
    "fetch_article",
    "filter_by_period",
    "html_to_markdown",
    "is_mp_article_url",
    "list_all_articles",
    "parse_meta",
    "parse_period",
    "render_markdown",
    "sanitize_filename",
    "search_account",
    "verify_auth_key",
]

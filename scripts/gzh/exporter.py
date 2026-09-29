"""本地导出：把抓到的文章写成知识库 Markdown。

目录结构（可直接放进 Obsidian vault）：
    <知识库根>/<公众号名>/
      ├── index.md              索引（表格，按时间倒序）
      ├── articles/
      │   └── 2023-01-16-标题.md
      ├── images/
      │   └── <hash>.jpg
      └── .gzh-index.json       增量去重用的元数据
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests

from .converter import guess_extension, html_to_markdown, sanitize_filename
from .fetcher import ArticleMeta

DEFAULT_KB_ROOT = "01_内容生产HQ/公众号素材库"

_IMG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://mp.weixin.qq.com/",
}


@dataclass
class ExportedArticle:
    """一篇已导出的文章记录。"""

    title: str
    account: str
    author: str
    publish_date: str
    publish_ts: int
    url: str
    path: str                 # 相对知识库根的 Markdown 路径
    images: int = 0
    is_original: bool = False
    channel: str = ""
    digest: str = ""
    tags: list[str] = field(default_factory=list)
    fetched_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _image_name(url: str, content_type: str = "") -> str:
    ext = guess_extension(url, content_type)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return f"{digest}{ext}"


def download_images(urls: list[str], image_dir: Path, timeout: int = 25,
                    session: requests.Session | None = None,
                    link_prefix: str = "../images") -> dict[str, str]:
    """下载图片到 image_dir，返回 {原始URL: 相对链接}。

    link_prefix 必须相对「Markdown 文件所在目录」。
    文章写在 articles/ 下、图片放在同级 images/ 下，所以默认是 ../images/xxx.jpg。
    """
    mapping: dict[str, str] = {}
    if not urls:
        return mapping

    image_dir.mkdir(parents=True, exist_ok=True)
    getter = (session or requests).get
    prefix = link_prefix.rstrip("/")

    for url in urls:
        try:
            resp = getter(url, headers=_IMG_HEADERS, timeout=timeout)
            if resp.status_code != 200 or not resp.content:
                continue
            name = _image_name(url, resp.headers.get("Content-Type", ""))
            target = image_dir / name
            if not target.exists():
                target.write_bytes(resp.content)
            mapping[url] = f"{prefix}/{name}"
        except Exception:  # noqa: BLE001 - 单张图片失败不影响正文
            continue

    return mapping


def build_frontmatter(meta: ArticleMeta, tags: list[str] | None = None) -> str:
    """生成 Obsidian 兼容的 YAML frontmatter。"""
    def esc(value: str) -> str:
        return (value or "").replace('"', '\\"').replace("\n", " ").strip()

    all_tags = ["公众号", "公众号文章"]
    if meta.account:
        all_tags.append(meta.account)
    if meta.is_original:
        all_tags.append("原创")
    for extra in tags or []:
        if extra and extra not in all_tags:
            all_tags.append(extra)

    lines = [
        "---",
        f'title: "{esc(meta.title)}"',
        f'account: "{esc(meta.account)}"',
        f'author: "{esc(meta.author)}"',
        f'publish_date: "{meta.publish_date}"',
        f'source_url: "{esc(meta.url)}"',
        "source: 微信公众号",
        f'original: {str(meta.is_original).lower()}',
        f'fetched_at: "{time.strftime("%Y-%m-%d %H:%M:%S")}"',
        "tags:",
    ]
    for tag in all_tags:
        lines.append(f"  - {tag}")
    lines.append("---")
    return "\n".join(lines)


def render_markdown(meta: ArticleMeta, body_md: str, tags: list[str] | None = None) -> str:
    """拼出完整的 Markdown 文档。"""
    parts = [build_frontmatter(meta, tags), ""]

    parts.append(f"# {meta.title or '未命名文章'}")
    parts.append("")

    info = []
    if meta.account:
        info.append(f"**公众号**：{meta.account}")
    if meta.author:
        info.append(f"**作者**：{meta.author}")
    if meta.publish_date:
        info.append(f"**发布时间**：{meta.publish_datetime or meta.publish_date}")
    if meta.is_original:
        info.append("**原创**：是")
    if info:
        parts.append("> " + " ｜ ".join(info))
        parts.append("")

    if meta.url:
        parts.append(f"> 原文链接：{meta.url}")
        parts.append("")

    parts.append("---")
    parts.append("")
    parts.append(body_md)
    parts.append("")

    return "\n".join(parts)


def article_filename(meta: ArticleMeta, index: int = 0) -> str:
    """生成 Markdown 文件名：日期-标题.md。"""
    date_part = meta.publish_date or time.strftime("%Y-%m-%d")
    title_part = sanitize_filename(meta.title, fallback=f"article-{index:03d}")
    return f"{date_part}-{title_part}.md"


class KnowledgeBase:
    """一个知识库目录，按公众号分文件夹。"""

    def __init__(self, root: str | Path, account: str = ""):
        self.root = Path(root).expanduser().resolve()
        self.account = sanitize_filename(account, fallback="未命名公众号")
        self.dir = self.root / self.account
        self.articles_dir = self.dir / "articles"
        self.images_dir = self.dir / "images"
        self.index_path = self.dir / ".gzh-index.json"

    def ensure_dirs(self) -> None:
        self.articles_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)

    def load_index(self) -> dict[str, dict]:
        """读取已导出记录，key 用原文链接，用于增量去重。"""
        if not self.index_path.exists():
            return {}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            return {item["url"]: item for item in data.get("articles", []) if item.get("url")}
        except (json.JSONDecodeError, KeyError, OSError):
            return {}

    def save_index(self, articles: list[ExportedArticle]) -> None:
        payload = {
            "account": self.account,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(articles),
            "articles": [a.to_dict() for a in articles],
        }
        self.index_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def export_article(self, meta: ArticleMeta, content_html: str, images: list[str],
                       tags: list[str] | None = None, download_image_files: bool = True,
                       session: requests.Session | None = None) -> ExportedArticle:
        """导出一篇文章：下载图片 → 转 Markdown → 写文件。"""
        self.ensure_dirs()

        image_map: dict[str, str] = {}
        if download_image_files and images:
            image_map = download_images(images, self.images_dir, session=session)

        body_md = html_to_markdown(content_html, image_map=image_map)
        doc = render_markdown(meta, body_md, tags=tags)

        # 幂等：同一篇文章重复导出时覆盖原文件，而不是生成 -2/-3 副本。
        target: Path | None = None
        if meta.url:
            prior = self.load_index().get(meta.url)
            if prior and prior.get("path"):
                candidate = self.root / prior["path"]
                if candidate.parent == self.articles_dir:
                    target = candidate

        if target is None:
            target = self.articles_dir / article_filename(meta)
            # 同名但不同文（标题+日期都撞车）时才加序号，避免覆盖别人的文章
            if target.exists():
                stem, suffix = target.stem, target.suffix
                n = 2
                while target.exists():
                    target = self.articles_dir / f"{stem}-{n}{suffix}"
                    n += 1

        target.write_text(doc, encoding="utf-8")

        return ExportedArticle(
            title=meta.title,
            account=meta.account or self.account,
            author=meta.author,
            publish_date=meta.publish_date,
            publish_ts=meta.publish_ts,
            url=meta.url,
            path=str(target.relative_to(self.root)),
            images=len(image_map),
            is_original=meta.is_original,
            channel="",
            digest=meta.digest[:120],
            tags=tags or [],
            fetched_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def write_index_md(self, articles: list[ExportedArticle], period_label: str = "") -> Path:
        """生成 index.md，按发布时间倒序。"""
        self.ensure_dirs()
        ordered = sorted(articles, key=lambda a: a.publish_ts or 0, reverse=True)

        lines = [
            "---",
            f'title: "{self.account} 文章库"',
            f'account: "{self.account}"',
            "tags:",
            "  - 公众号",
            "  - 公众号文章",
            "  - 文章索引",
            "---",
            "",
            f"# {self.account} · 文章库",
            "",
            f"> 共 {len(ordered)} 篇 ｜ 期限：{period_label or '全部历史'} ｜ "
            f"更新：{time.strftime('%Y-%m-%d %H:%M')}",
            "",
            "| 日期 | 标题 | 作者 | 图片 | 原创 |",
            "| --- | --- | --- | --- | --- |",
        ]

        for art in ordered:
            title = (art.title or "").replace("|", "\\|")
            # art.path 相对知识库根（含 <公众号>/ 前缀），但 index.md 就在该前缀目录里，
            # 链接必须相对 index.md 本身，否则 Obsidian 里点不开。
            rel = art.path
            prefix = f"{self.account}/"
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
            link = rel.replace(" ", "%20")
            original = "✅" if art.is_original else ""
            lines.append(
                f"| {art.publish_date or '-'} | [{title}]({link}) | "
                f"{art.author or '-'} | {art.images} | {original} |"
            )

        lines.append("")
        target = self.dir / "index.md"
        target.write_text("\n".join(lines), encoding="utf-8")
        return target


def write_manifest(kb: KnowledgeBase, articles: list[ExportedArticle],
                   period_label: str = "", extra: dict | None = None) -> Path:
    """写一份运行清单，便于验收与排错。"""
    kb.ensure_dirs()
    payload = {
        "account": kb.account,
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "period": period_label,
        "count": len(articles),
        "knowledge_base_dir": str(kb.dir),
        "articles": [a.to_dict() for a in articles],
    }
    if extra:
        payload.update(extra)
    target = kb.dir / "manifest.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


# ---------------------------------------------------------------- 索引重建

def _parse_frontmatter(text: str) -> dict:
    """从 Markdown 的 YAML frontmatter 里取字段。

    只支持本项目自己写出的简单标量/列表格式，不引入 YAML 依赖。
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    data: dict = {}
    key = None
    for raw in text[3:end].splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("- ") and key:
            data.setdefault(key, [])
            if isinstance(data[key], list):
                data[key].append(line.lstrip()[2:].strip().strip('"'))
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        key = k.strip()
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            data[key] = [x.strip().strip('"') for x in v[1:-1].split(",") if x.strip()]
        elif v:
            data[key] = v.strip('"')
        else:
            data[key] = []
    return data


def rebuild_index_from_files(kb: KnowledgeBase) -> list[ExportedArticle]:
    """扫描 articles/ 目录，从每篇的 frontmatter 重建索引。

    ## 为什么需要这个

    索引（`.gzh-index.json` / `index.md` / `manifest.json`）原来只在**导出流程结束时**
    写一次。一旦中途被打断（Ctrl-C、限流中断、进程被杀），磁盘上会留下一批文章文件，
    但索引里没有它们 —— 表现为「文件明明在，index.md 里却查不到」。

    这个函数让索引可以从文件**反推**出来，于是「中断」不再是数据丢失，
    只是需要补跑一次重建。

    同时它也是**幂等**的：重复跑不会重复计数。
    """
    kb.ensure_dirs()
    articles: list[ExportedArticle] = []

    for md in sorted(kb.articles_dir.glob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        if not fm:
            continue

        # 图片数：数一下正文里引用了几张
        images = len(re.findall(r"!\[[^\]]*\]\(\.\./images/", text))

        tags = fm.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]

        publish_date = str(fm.get("publish_date") or "")
        publish_ts = 0
        if publish_date:
            try:
                publish_ts = int(time.mktime(time.strptime(publish_date[:10], "%Y-%m-%d")))
            except ValueError:
                publish_ts = 0

        articles.append(ExportedArticle(
            title=str(fm.get("title") or md.stem),
            account=str(fm.get("account") or kb.account),
            author=str(fm.get("author") or ""),
            publish_date=publish_date,
            publish_ts=publish_ts,
            url=str(fm.get("source_url") or ""),
            path=f"articles/{md.name}",
            images=images,
            is_original=str(fm.get("original") or "").lower() in ("true", "是", "1"),
            channel="rebuild",
            digest="",
            tags=tags,
            fetched_at=str(fm.get("fetched_at") or ""),
        ))

    articles.sort(key=lambda a: (a.publish_date, a.title), reverse=True)
    return articles

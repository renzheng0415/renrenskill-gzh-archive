"""HTML → Markdown 转换，针对微信公众号正文优化。

微信正文的特点：
- 大量嵌套 <section>，没有语义化的 <p>
- 图片用 data-src 懒加载，需要替换成本地路径
- 行内样式噪音极大（style/class/data-* 属性），必须剥掉
- 空段落、&nbsp;、连续空行很多
"""

from __future__ import annotations

import html as html_mod
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

# 微信正文里需要整块丢弃的元素
_DROP_TAGS = {"script", "style", "svg", "noscript", "iframe", "mp-common-videosnap",
              "mpvoice", "mp-common-profile", "qqmusic", "mpcpc"}

# 剥掉所有属性，只保留这几个
_KEEP_ATTRS = {"href", "src", "alt", "title"}


def _clean_soup(content_html: str) -> BeautifulSoup:
    soup = BeautifulSoup(content_html, "lxml")

    for tag in soup.find_all(list(_DROP_TAGS)):
        tag.decompose()

    # 先把微信懒加载的图片地址搬回 src —— 这一步必须在剥属性之前，
    # 否则 data-src 会被当成样式噪音丢掉，正文里的图片会全部消失。
    for img in soup.find_all("img"):
        if not img.get("src"):
            for key in ("data-src", "data-original", "data-backsrc"):
                if img.get(key):
                    img["src"] = img[key]
                    break

    # 再剥属性（保留链接与图片的必要属性）
    for tag in soup.find_all(True):
        if tag.name in ("a", "img"):
            keep = {k: v for k, v in tag.attrs.items() if k in _KEEP_ATTRS}
            tag.attrs = keep
        else:
            tag.attrs = {}

    return soup


def _normalize_ws(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _inline(node: Tag) -> str:
    """把一个元素的内容渲染成行内 Markdown 文本。"""
    parts: list[str] = []

    for child in node.children:
        if isinstance(child, NavigableString):
            parts.append(_normalize_ws(str(child)))
            continue
        if not isinstance(child, Tag):
            continue

        name = child.name.lower()
        if name in _DROP_TAGS:
            continue

        if name == "br":
            parts.append("\n")
        elif name == "img":
            src = child.get("src", "")
            alt = _normalize_ws(child.get("alt", "")) or "图片"
            parts.append(f"\n\n![{alt}]({src})\n\n" if src else "")
        elif name == "a":
            href = child.get("href", "")
            label = _inline(child)
            if href and label and not href.startswith("javascript:"):
                parts.append(f"[{label}]({href})")
            else:
                parts.append(label)
        elif name in ("strong", "b"):
            inner = _inline(child)
            parts.append(f"**{inner}**" if inner else "")
        elif name in ("em", "i"):
            inner = _inline(child)
            parts.append(f"*{inner}*" if inner else "")
        elif name == "code":
            inner = _inline(child)
            parts.append(f"`{inner}`" if inner else "")
        else:
            parts.append(_inline(child))

    text = "".join(parts)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    return text.strip()


def _block(node: Tag, depth: int = 0) -> list[str]:
    """把一个块级元素渲染成 Markdown 块列表。"""
    out: list[str] = []

    for child in node.children:
        if isinstance(child, NavigableString):
            txt = _normalize_ws(str(child))
            if txt:
                out.append(txt)
            continue
        if not isinstance(child, Tag):
            continue

        name = child.name.lower()
        if name in _DROP_TAGS:
            continue

        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(name[1])
            txt = _inline(child)
            if txt:
                out.append("#" * level + " " + txt)

        elif name == "img":
            src = child.get("src", "")
            alt = _normalize_ws(child.get("alt", "")) or "图片"
            if src:
                out.append(f"![{alt}]({src})")

        elif name == "blockquote":
            inner = "\n".join(_block(child, depth + 1))
            if inner:
                out.append("\n".join("> " + line if line else ">" for line in inner.split("\n")))

        elif name == "pre":
            code = child.get_text()
            out.append("```\n" + code.strip("\n") + "\n```")

        elif name in ("ul", "ol"):
            ordered = name == "ol"
            for i, li in enumerate(child.find_all("li", recursive=False), 1):
                txt = _inline(li)
                if txt:
                    bullet = f"{i}." if ordered else "-"
                    out.append(f"{bullet} {txt}")

        elif name == "hr":
            out.append("---")

        elif name in ("table",):
            rows = []
            for tr in child.find_all("tr"):
                cells = [_inline(td) for td in tr.find_all(["td", "th"])]
                if cells:
                    rows.append(cells)
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                head = "| " + " | ".join(rows[0]) + " |"
                sep = "| " + " | ".join(["---"] * width) + " |"
                body = ["| " + " | ".join(r) + " |" for r in rows[1:]]
                out.append("\n".join([head, sep] + body))

        elif name in ("p", "section", "div", "article", "span", "figure", "figcaption", "td", "li"):
            # section/div 是微信的主力容器：若内部还有块级子元素就递归，否则当段落
            has_block_child = any(
                isinstance(c, Tag) and c.name.lower() in
                ("p", "section", "div", "img", "h1", "h2", "h3", "h4", "h5", "h6",
                 "ul", "ol", "blockquote", "table", "pre", "hr", "figure")
                for c in child.children
            )
            if has_block_child:
                out.extend(_block(child, depth + 1))
            else:
                txt = _inline(child)
                if txt:
                    out.append(txt)

        else:
            out.extend(_block(child, depth + 1))

    return out


def html_to_markdown(content_html: str, image_map: dict[str, str] | None = None) -> str:
    """把微信正文 HTML 转成干净的 Markdown。

    image_map: 原始图片 URL → 本地相对路径，用于把远程图换成知识库里的本地图。
    """
    if not content_html or not content_html.strip():
        return ""

    soup = _clean_soup(content_html)
    root = soup.body or soup

    blocks = _block(root)
    text = "\n\n".join(b for b in blocks if b is not None)

    # 合并过多空行，规整图片周围的空白
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\n{2,}(!\[)", r"\n\n\1", text)
    text = text.strip()

    if image_map:
        for remote, local in image_map.items():
            text = text.replace(f"]({remote})", f"]({local})")

    return text


def guess_extension(url: str, content_type: str = "") -> str:
    """根据 URL 或 Content-Type 猜图片扩展名。"""
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        mapping = {
            "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
            "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
            "image/bmp": ".bmp",
        }
        if ct in mapping:
            return mapping[ct]

    wx_fmt = re.search(r"wx_fmt=([a-zA-Z0-9]+)", url)
    if wx_fmt:
        fmt = wx_fmt.group(1).lower()
        return {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png",
                "gif": ".gif", "webp": ".webp", "svg": ".svg"}.get(fmt, f".{fmt}")

    path = urlparse(url).path
    ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    if ext in ("jpg", "jpeg", "png", "gif", "webp", "svg", "bmp"):
        return ".jpg" if ext == "jpeg" else f".{ext}"
    return ".jpg"


def sanitize_filename(name: str, fallback: str = "untitled", max_len: int = 80) -> str:
    """把标题转成安全的文件名。"""
    name = html_mod.unescape(name or "").strip()
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or fallback

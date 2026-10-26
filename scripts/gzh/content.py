"""Parse WeChat article and album HTML without executing embedded scripts."""
from __future__ import annotations
import html
import re
from .fetcher import FetchError, FetchResult, extract_content_html, extract_images, parse_meta, _meta_content


def decode_js_text(value: str) -> str:
    value = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m[1], 16)), value)
    value = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), value)
    return html.unescape(value.replace(r"\n", "\n").replace(r"\r", "\r")
                         .replace(r"\'", "'").replace(r'\"', '"').replace(r"\/", "/"))


def parse_content(raw: str, url: str = "") -> FetchResult:
    source = _meta_content(raw, "og:url") or url
    if not source.startswith("https://mp.weixin.qq.com/s"):
        raise FetchError("正文缺少有效微信原文链接")
    meta = parse_meta(raw, source)
    body = extract_content_html(raw)
    if not body and "picture_page_info_list:" in raw:
        description = decode_js_text(_meta_content(raw, "og:description") or _meta_content(raw, "description"))
        section = raw.split("picture_page_info_list:", 1)[1].split("window.picture_page_info_list", 1)[0]
        urls = list(dict.fromkeys(decode_js_text(u) for u in re.findall(
            r"\{\s*cdn_url:\s*'([^']+)'\s*,\s*width:", section)))
        urls = [u for u in urls if u.startswith(("https://", "http://"))]
        body = "".join("<p>" + html.escape(p) + "</p>" for p in description.splitlines() if p.strip())
        body += "".join('<p><img data-src="' + html.escape(u, quote=True) + '"></p>' for u in urls)
    if not body.strip():
        raise FetchError("正文为空或页面类型尚未支持；保留失败记录，不生成成功占位文件")
    return FetchResult(meta=meta, html=raw, content_html=body, images=extract_images(body), channel="weread-content")

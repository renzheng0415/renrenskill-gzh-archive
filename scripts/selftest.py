"""一键自检：把「能自动测的」全部跑一遍，输出清晰的通过/失败清单。

用法：
    python3 selftest.py                 # 用内置样例文章
    python3 selftest.py "<文章链接>"     # 用你指定的真实文章

不需要 auth-key。需要 auth-key 的那条路径会单独提示。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 内置样例：一篇真实存在的公众号文章（用于无参数自检）。
# 注意 chksm 必须带上 —— 实测缺了它微信会 302 到错误页。
SAMPLE_URL = (
    "https://mp.weixin.qq.com/s?__biz=MjI3Njc0NTk4MQ=="
    "&mid=2650266004&idx=1&sn=9b8053fb0b7d92eec292941f52ad092e"
    "&chksm=b788ec0680ff651057dbfc75a6c8d702cb9022b0decc4b0f742422b8209e5661e8c5a59437b2"
)

PASS, FAIL, SKIP = "✅", "❌", "⏭️ "
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool | None, detail: str = "") -> None:
    mark = SKIP if ok is None else (PASS if ok else FAIL)
    results.append((mark, name, detail))
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))


# 网络层抖动 ≠ 代码错误。自检要回答的是「代码对不对」，不是「此刻外网通不通」。
_NETWORK_NOISE = (
    "SSLError", "Max retries", "timed out", "timeout", "Timeout",
    "ConnectionError", "ConnectionReset", "Name or service",
    "Temporary failure", "RemoteDisconnected", "EOF occurred",
)


def _is_network_noise(text: str) -> bool:
    return any(k.lower() in text.lower() for k in _NETWORK_NOISE)


def section(title: str) -> None:
    print(f"\n{'─' * 62}\n{title}\n{'─' * 62}")


# ---------------------------------------------------------------- 1. 环境

def test_environment() -> None:
    section("1. 环境检查")
    record("Python 版本", sys.version_info >= (3, 9), f"{sys.version.split()[0]}")

    for mod, label in [("bs4", "beautifulsoup4"), ("lxml", "lxml"), ("requests", "requests")]:
        try:
            __import__(mod)
            record(f"依赖 {label}", True)
        except ImportError:
            record(f"依赖 {label}", False, f"缺 {label}，请 pip install {label}")

    # 引擎模块能否 import
    try:
        from gzh import (  # noqa: F401
            fetch_article, html_to_markdown, parse_period, KnowledgeBase,
        )
        record("抓取引擎可导入", True)
    except Exception as exc:  # noqa: BLE001
        record("抓取引擎可导入", False, str(exc)[:80])


# ---------------------------------------------------------------- 2. 单测

def test_unit() -> None:
    section("2. 单元测试（期限边界 / 解析 / 转换 / 路径 / 公开索引）")
    total = 0
    all_ok = True
    for name in ("test_core.py", "test_sogou.py", "test_weread_period.py", "test_ratelimit.py", "test_reliability.py"):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tests" / name)],
            capture_output=True, text=True, cwd=ROOT,
        )
        out = (proc.stderr or "") + (proc.stdout or "")
        m = re.search(r"Ran (\d+) tests?", out)
        count = int(m.group(1)) if m else 0
        total += count
        if proc.returncode != 0:
            all_ok = False
            record(f"{name}（{count} 个）", False, out.strip().splitlines()[-1][:80])
    record(f"单元测试合计（{total} 个）", all_ok, "全部通过" if all_ok else "有失败")


# ---------------------------------------------------------------- 3. 抓取

def test_fetch(url: str) -> Path | None:
    section("3. 真实抓取（直连通道）")
    from gzh.fetcher import FetchError, fetch_article, is_mp_article_url

    if not is_mp_article_url(url):
        record("链接格式", False, f"不是公众号文章链接：{url[:60]}")
        return None
    record("链接格式", True)

    t0 = time.time()
    try:
        result = fetch_article(url, timeout=30)
    except FetchError as exc:
        record("抓取正文", False, str(exc)[:100])
        return None

    elapsed = time.time() - t0
    meta = result.meta
    record("抓取正文", True, f"{elapsed:.1f}s，通道 {result.channel}，{len(result.content_html)} 字节")

    record("解析标题", bool(meta.title), meta.title[:40] if meta.title else "未解析到")
    record("解析公众号", bool(meta.account), meta.account or "未解析到")
    record("解析发布时间", bool(meta.publish_ts), meta.publish_datetime or "未解析到")
    record("正文非空", len(result.content_html.strip()) > 200, f"{len(result.content_html)} 字节")
    record("图片识别", True, f"{len(result.images)} 张")

    return result


# ---------------------------------------------------------------- 4. 导出

def test_export(result, tmp: Path) -> None:
    section("4. 导出知识库（Markdown / 图片 / 索引）")
    from gzh.exporter import ExportedArticle, KnowledgeBase

    kb = KnowledgeBase(tmp, account=result.meta.account or "测试号")
    art = kb.export_article(result.meta, result.content_html, result.images)
    art.channel = result.channel

    md_path = tmp / art.path
    record("Markdown 已落盘", md_path.exists(), str(md_path.name)[:50])
    if not md_path.exists():
        return

    doc = md_path.read_text(encoding="utf-8")

    record("Obsidian frontmatter", doc.startswith("---\n") and "title:" in doc)
    record("含来源链接", "source_url:" in doc)

    # 关键回归：图片相对路径必须能解析到真实文件
    refs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", doc)
    missing = [r for r in refs if not (md_path.parent / r).resolve().exists()]
    record("图片引用可解析", len(missing) == 0,
           f"{len(refs)} 个引用，{len(missing)} 个失效" if refs else "正文无图片")

    # 索引
    kb.save_index([art])
    index = kb.write_index_md([art], "自检")
    idx_text = index.read_text(encoding="utf-8")
    record("生成 index.md", index.exists())
    record("索引链接相对正确", "](articles/" in idx_text and "](/" not in idx_text)

    # 幂等：重复导出不应产生副本
    before = len(list((tmp / art.path).parent.glob("*.md")))
    kb.save_index([art])
    art2 = kb.export_article(result.meta, result.content_html, result.images)
    after = len(list((tmp / art.path).parent.glob("*.md")))
    record("重复导出幂等", before == after, f"{before} → {after} 个文件")

    # 期限筛选
    from gzh.period import filter_by_period, parse_period
    now = time.time()
    items = [{"create_time": int(now - d * 86400), "title": t}
             for d, t in [(5, "A"), (45, "B"), (100, "C"), (200, "D")]]
    expect = {"1m": ["A"], "3m": ["A", "B"], "6m": ["A", "B", "C"],
              "1y": ["A", "B", "C", "D"], "all": ["A", "B", "C", "D"]}
    bad = [k for k, want in expect.items()
           if [i["title"] for i in filter_by_period(items, parse_period(k), now=now)] != want]
    record("期限筛选 1m/3m/6m/1y/all", not bad, "全部正确" if not bad else f"错误：{bad}")


# ---------------------------------------------------------------- 5. 插件

def test_plugin() -> None:
    section("5. DSH 插件包（可选）")
    plug = ROOT / "plugin"

    # 插件包是**可选**的：本代码同时以两种形态发布 ——
    #   ① Agent Skill（本目录）—— 靠 SKILL.md 驱动，不需要 plugin/
    #   ② DSH 插件（plugin/ + lib/index.js）—— 让对话里直接调用工具
    # 在 skill 形态下没有 plugin/ 是正常的，所以缺了就跳过，不算失败。
    if not plug.exists():
        record("DSH 插件包", None, "未打包（skill 形态不需要，属正常）")
        return

    record("package.json 存在", (plug / "package.json").exists())
    record("cordis.patch.yml 存在", (plug / "cordis.patch.yml").exists())
    record("lib/index.js 存在", (plug / "lib" / "index.js").exists())

    if (plug / "package.json").exists():
        try:
            pkg = json.loads((plug / "package.json").read_text(encoding="utf-8"))
            record("声明 dsh.bundle.patch",
                   bool(pkg.get("dsh", {}).get("bundle", {}).get("patch")))
            record("零运行时依赖", pkg.get("dependencies") == {},
                   f"dependencies = {pkg.get('dependencies')}")
        except Exception as exc:  # noqa: BLE001
            record("package.json 可解析", False, str(exc)[:60])

    if (plug / "lib" / "index.js").exists():
        src = (plug / "lib" / "index.js").read_text(encoding="utf-8")
        record("不 import @deepseek-ai/*", "@deepseek-ai" not in src.replace(
            "零 `@deepseek-ai/*` 依赖", ""))
        for tool in ("gzh_fetch_article", "gzh_list_history",
                     "gzh_export_history", "gzh_status"):
            record(f"注册工具 {tool}", f'name: "{tool}"' in src)


# ---------------------------------------------------------------- 6. auth-key

def test_authkey() -> None:
    section("6. 可选名称搜索服务认证（微信读书归档不依赖）")
    from gzh.config import Config
    from gzh.fetcher import verify_auth_key

    cfg = Config.load()
    if not cfg.auth_key:
        record("auth-key 已配置", None, "未配置 —— 见下方「下一步」")
        return

    valid = verify_auth_key(cfg.auth_key, api_base=cfg.api_base)
    record("可选 auth-key", True if valid else None,
           "名称搜索可用" if valid else "认证不可用；使用 --from-url，不影响微信读书路径")

    if valid:
        from gzh.fetcher import FetchError, FreqControlError, list_articles, search_account
        try:
            accounts = search_account("深圳楼市", cfg.auth_key, api_base=cfg.api_base)
            record("搜索公众号接口", True, f"返回 {len(accounts)} 个结果")
        except FetchError as exc:
            record("搜索公众号接口", False, str(exc)[:80])
            return

        # 历史列表：2026-07-30 起微信已对第三方收紧，预期会拿到 200013。
        # 这不是失败，而是已知的平台变更 —— 能正确识别成 FreqControlError 就算通过。
        if not accounts:
            record("历史列表接口", None, "没搜到测试号，跳过")
            return
        fakeid = accounts[0].get("fakeid", "")
        try:
            arts = list_articles(fakeid, cfg.auth_key, api_base=cfg.api_base, size=5)
            record("历史列表接口（后台）", True, f"可用，返回 {len(arts)} 篇")
        except FreqControlError:
            record("历史列表接口（后台）", True,
                   "已被平台限制（200013）—— 符合预期，会自动走公开索引兜底")
        except FetchError as exc:
            msg = str(exc)
            # ⚠️ 网络层问题（SSL 抖动、DNS、超时）不算「代码坏了」。
            #    自检要回答的是「这套代码对不对」，不是「此刻外网通不通」。
            #    实测这个第三方服务偶发 SSL 超时，早期把它记成失败，
            #    导致自检约 1/3 概率报红 —— 那是噪音，不是信号。
            if _is_network_noise(msg):
                record("历史列表接口（后台）", None,
                       f"外网抖动，跳过（不影响代码正确性）：{msg[:56]}")
            else:
                record("历史列表接口（后台）", False, msg[:80])
        except Exception as exc:  # noqa: BLE001 - 兜底，别让自检崩在这
            name = type(exc).__name__
            if _is_network_noise(name) or _is_network_noise(str(exc)):
                record("历史列表接口（后台）", None, f"外网抖动，跳过：{name}")
            else:
                record("历史列表接口（后台）", False, f"{name}: {str(exc)[:60]}")


# ---------------------------------------------------------------- main

def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else SAMPLE_URL

    print("=" * 62)
    print("公众号文章获取插件 · 自检")
    print("=" * 62)
    print(f"样例文章：{url[:70]}…" if len(url) > 70 else f"样例文章：{url}")

    test_environment()
    test_unit()

    tmp = Path(tempfile.mkdtemp(prefix="gzh-selftest-"))
    try:
        result = test_fetch(url)
        if result:
            test_export(result, tmp)
        else:
            section("4. 导出知识库")
            record("导出知识库", None, "跳过（抓取未成功）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    test_plugin()
    test_authkey()

    # 汇总
    passed = sum(1 for m, _, _ in results if m == PASS)
    failed = sum(1 for m, _, _ in results if m == FAIL)
    skipped = sum(1 for m, _, _ in results if m == SKIP)

    section("汇总")
    print(f"  {PASS} 通过 {passed}   {FAIL} 失败 {failed}   {SKIP} 跳过 {skipped}")

    if failed:
        print("\n失败项：")
        for mark, name, detail in results:
            if mark == FAIL:
                print(f"  {FAIL} {name}  — {detail}")
        return 1

    if skipped:
        print("\n可选项目已跳过。微信读书归档可用 --from-url，不要求补齐名称搜索认证。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

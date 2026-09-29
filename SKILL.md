---
name: renrenskill-gzh-archive
description: 把微信公众号某个博主的历史文章全量归档成本地 Markdown 知识库（含图片、frontmatter、索引）。2026 年微信封禁后台列表接口后唯一可用的全量路径是微信读书 + 专用 Chrome。支持按期限筛选、断点续跑、三通道正文抓取（含 JS 渲染页）。用户提到 renrenskill、抓公众号、导出公众号文章、公众号归档、备份某个号、把某博主的文章存进知识库、公众号转 Markdown、批量下载公众号历史文章时使用。WeChat official-account full-history archiver to local Markdown knowledge base.
version: 1.0.0
---

# 公众号文章归档（renrenskill）

把任意微信公众号的历史文章**全量**抓下来，转成本地 Markdown 知识库。

**实测能力**：单号 502 篇 / 4.2 年 / 51 个月零缺口 / 图片 2704 张，约 20 分钟跑完，0 次风控。

## 路径解析（先读这条）

本文档里的 `{baseDir}` 指**本 SKILL.md 所在目录**。

**执行任何命令前先把它换成绝对路径：**

```bash
BASE_DIR="$(cd "$(dirname "$(find ~/.agents/skills ~/.claude/skills ~/.qwen/skills -name SKILL.md -path '*renrenskill-gzh-archive*' 2>/dev/null | head -1)")" && pwd)"
echo "$BASE_DIR"
```

之后统一用 `$BASE_DIR`：

```bash
bash "$BASE_DIR/scripts/gzh.sh" --help
```

`scripts/gzh.sh` 是**唯一入口** —— 它自己定位代码、自动补 `requests` 依赖。
所以不用管 `PYTHONPATH`，也不用管当前在哪个目录。

---

## 先讲清楚：2026 年之后这件事为什么变难了

**微信在 2026-07-30 掐掉了「公众号后台跨账号文章列表」接口**（`ret=200013 freq control`）。

后果：**网上绝大多数公众号导出工具同时失效** —— 它们全都依赖那一个接口
（wechat-article-exporter、wechat-download-api、多数 RSS 服务…）。

**「换个工具」解决不了，必须换数据源。**

本 Skill 走的是**微信读书**这条独立管线（微信读书把公众号当「书」管理，与公众号后台无关）。
这条路在 200013 之后**实测仍然可用**，是已知唯一能翻到开号第一篇的路径。

> 详细调研过程见 `references/platform-change.md`。

---

## 开工三步

1. **确认前置条件**（缺一不可，见下节「前置检查」）
2. **拿到目标公众号的一篇文章链接** → 自动解析 `bookId`
3. **跑全量抓取** → 断点续跑，跑完为止

---

## 前置检查

**必须先做这一步，否则抓不了。**

```bash
# ① 专用 Chrome 是否在跑
curl -s -m 3 http://127.0.0.1:9333/json/version

# ② 微信读书抓取工具是否装了
ls -d ~/.weread-mp-fetcher/tool

# ③ 浏览器渲染通道是否可用（端到端，最可靠）
bash {baseDir}/scripts/gzh.sh article "https://mp.weixin.qq.com/s/hNtnQsJ_T7GIdBQ5-LUoMQ" --no-images
```

**任一项不满足时怎么办：**

| 缺什么 | 处理 |
|---|---|
| 专用 Chrome 没开 | 让用户跑 `bash ~/.weread-mp-fetcher/start-chrome.sh`，然后**扫码登录微信读书**（一次性） |
| 工具没装 | 见 `references/setup.md` 的安装步骤 |
| Chrome 开了但没登录 | `--shelf` 会报 `-2010 用户不存在` → 让用户在 Chrome 窗口里重新扫码 |

**⚠️ 抓取全程那个 Chrome 要开着** —— 微信正在把文章页改成 JS 动态渲染，
被改过的页面只有浏览器渲染通道能抓到正文。

---

## 主流程：全量归档一个号

**三种定位目标号的方式**（按推荐度排序）：

```bash
# ① 给一篇文章链接（最推荐：不依赖任何 key）
bash {baseDir}/scripts/gzh.sh crawl \
  --from-url "https://mp.weixin.qq.com/s/xxxxx" \
  --interval 25 --kb "<知识库根目录>"

# ② 给公众号名字（走 searchbiz，需要 auth-key —— 但该接口至今仍可用）
bash {baseDir}/scripts/gzh.sh crawl \
  --from-name "某某研究院" \
  --interval 25 --kb "<知识库根目录>"

# ③ 已知 bookId 时直接给
bash {baseDir}/scripts/gzh.sh crawl \
  --book-id MP_WXS_3079276420 --account "公众号名" \
  --interval 25 --kb "<知识库根目录>"
```

> **为什么 ② 还能用**：微信封的是「列历史文章」接口（200013），
> **不是「搜公众号」接口**。`searchbiz` 返回的 `fakeid` 就是 `__biz`，
> 直接换算出 `bookId`（`MP_WXS_` + base64解码）。实测：
> 例：某公众号 → `fakeid=MjM0NTY3ODkwMQ==` → `MP_WXS_2345678901`，与文章链接的换算结果一致。
>
> 同名号会有多个（实测搜一个名字常能搜到若干个近似号），
> 工具会列出候选并优先选**精确同名**的那个。

**跑不完不用管，再跑一次同样的命令** —— 会从断点接着跑。

**跑完后必须补一步**（否则可能静默缺文章）：

```bash
bash {baseDir}/scripts/gzh.sh backfill --account "<公众号名>" --kb "<知识库根目录>"
```

> **为什么必须 backfill**：断点文件记的是「URL 在列表里出现过」，
> 知识库文件是「正文已成功抓下」—— 两件事不一样。
> 中断过的号会留下差集，而续跑从更晚的 offset 开始，**那批永远不会被回头扫到**。
> 实测踩到过一次：丢了 18 篇。

---

## 命令速查

所有命令统一走包装器（自动定位代码、自动补依赖）：

```bash
GZH="bash {baseDir}/scripts/gzh.sh"

# 全量归档（主力）
$GZH crawl --from-url "<文章链接>" --interval 25 --kb "<知识库>"

# 补齐列表与知识库的差集（每次 crawl 后跑）
$GZH backfill --account "<公众号名>" --kb "<知识库>"

# 从磁盘 Markdown 重建索引（中断/异常后用）
$GZH rebuild --kb "<知识库>" --account "<公众号名>"

# 快速抓最近 N 页（不想等全量时）
$GZH weread --period 6m --pages 3 --kb "<知识库>"

# 抓单篇
$GZH article "<文章链接>" --kb "<知识库>"

# 批量抓一批链接
$GZH urls links.txt --kb "<知识库>"

# 看微信读书书架里有哪些号
$GZH weread --shelf

# 检查专用 Chrome 与页面状态（不消耗额度）
$GZH weread --probe
```

---

## 限频：已经做成了硬闸门，绕不过去

微信读书**有风控**。这不是「注意一下」的事，是**实测撞过两次**的事。
所以限流不再是文档里的纪律，而是**写在磁盘上的账本** —— 谁发请求都得先过它。

### 安全节奏（默认值，刻意保守）

| 限制 | 值 | 来历 |
|---|---|---|
| 两个请求间隔 | **≥ 25 秒** | 25 秒间距连续翻 25 页 → 0 风控（成功抓完 501 篇） |
| 每小时 | ≤ 40 个请求 | 比理论值更保守 |
| 每 24 小时 | ≤ 200 个请求 | 比理论值更保守 |
| 命中风控后 | **强制静默 15 分钟** | 防「失败立刻重试」把封禁拖长 |

### 为什么必须做成跨进程的

**两次风控都不是翻页造成的，是翻页循环之外的临时调用造成的：**

| 事故 | 节奏 | 结果 |
|---|---|---|
| ① 手写脚本探深度 | 7 个请求 / 18 秒 | `-2041`，约 25 分钟恢复 |
| ② 连续测多个号可用性 | 无间隔 | 全站 `-2012`，约 20 分钟恢复 |

这两次都是**独立进程**发起的 —— 进程内的计时器管不着它们。
所以账本写在 `~/.config/gzh/ratelimit.json`，**跨脚本共享**。

### 闸门钉在哪

钉在 `fetch_page()` —— **唯一的取页出口**。
不管谁调用（正式抓取、临时测试、将来新写的脚本），都先过闸。

实测验证（故意模拟事故 ①）：

```
offset=  0  耗时   0.6s
             ⏳ 距上次请求仅 1 秒，等 24 秒…
offset= 19  耗时  25.4s
             ⏳ 距上次请求仅 1 秒，等 24 秒…
offset= 39  耗时  26.8s
总耗时 53 秒（若无限流则是 2 秒）
```

**限流器自动插了等待，节奏被压回安全区间。**

### 看当前节奏

```bash
bash {baseDir}/scripts/gzh.sh pace
```

输出人话报告：最近 1 小时 / 24 小时发了多少、上次请求在多久前、现在能不能发。

### 三条铁律

1. **不要绕过闸门**。包括「我就测一下」—— 两次事故都是这么来的。
2. **风控是临时的**（15-30 分钟自愈），但**失败后别立刻重试**，限流器会自动拦。
3. **出错就停手**。工具作者原话：
   > 「写在文档里的纪律约束不住人，也约束不住 AI。」
   > —— 他防的就是我们干的事。

---

## 什么时候算「抓完了」

工具用三个信号判停（任一成立即停）：

1. 接口返回 `reviews: 0`（该 offset 之后没有群发了）
2. 返回的「最早时间」不再前移
3. 连续两页拿到的 URL 全都在已见集合里

**输出 `✅ 已翻到尽头` 才是真抓完。**

⚠️ 判据是启发式的，**验收时建议独立复核一次**：手动取一个更大的 offset
（比如当前末尾 offset + 20），确认确实返回 0 群发。

---

## 交付物形态

```
<知识库根>/<公众号名>/
├── articles/          每篇一个 .md（文件名：YYYY-MM-DD-标题.md）
├── images/            本地化图片（文件名：内容哈希）
├── index.md           文章索引表（日期/标题/作者/图片数/原创）
├── .gzh-index.json    结构化索引（程序读写）
├── manifest.json      本次运行清单（含失败明细）
└── .weread-crawl.json 断点状态（续跑用）
```

**Markdown 的 frontmatter**：

```yaml
---
title: "文章标题"
account: "公众号名"
author: "作者署名"
publish_date: "2026-01-15"
source_url: "https://mp.weixin.qq.com/s/..."
source: 微信公众号
original: true
tags: [公众号, 公众号文章, 公众号名, 原创]
---
```

图片引用是 `../images/xxx.png`（相对 Markdown 文件所在目录），**在 Obsidian 里可直接显示**。

---

## 验收标准

抓完必须逐项核对（缺一项都不算完成）：

```bash
KB="<知识库根>/<公众号名>"

# 1. 四者数量一致
ls "$KB/articles" | wc -l                              # 文件数
python3 -c "import json;print(json.load(open('$KB/.gzh-index.json'))['count'])"
python3 -c "import json;print(json.load(open('$KB/manifest.json'))['count'])"
grep -c '^| 20' "$KB/index.md"                         # 索引表行数、四者必须相同

# 2. 月度分布无缺口
ls "$KB/articles" | cut -c1-7 | sort | uniq -c

# 3. 抽查首尾两篇的 frontmatter 和图片能否显示
```

**失败要如实报告**，常见两种（都是永久性的，不是 bug）：

| 现象 | 含义 |
|---|---|
| 「根据作者隐私设置，无法查看该内容」 | 作者自己设了限制 |
| 视频帖无正文 | 只有视频没有文字 |

---

## 三条正文抓取通道

微信正在把文章页改成 **JS 动态渲染**（静态 HTML 里只有壳）。所以自动逐级兜底：

| 通道 | 说明 |
|---|---|
| `direct` | 直接请求 mp.weixin.qq.com（快） |
| `api` | 公共导出服务兜底 |
| `browser` | 用专用 Chrome 渲染（慢，但能救回 JS 渲染页） |

**判据**：静态 HTML 里找不到真正的 `id="js_content"` 元素（只在 JS 源码里出现），
就是动态渲染页 → 必须走 `browser`。

**因此抓取时 Chrome 必须开着**，否则这批文章会失败。

---

## 用户偏好与追问

**默认行为**（不要每次都问）：

- 知识库根目录：从 `~/.config/gzh/config.json` 的 `kb_root` 读；用户给了就用用户的
- 限频：用默认的保守值，**不要擅自调快**
- 图片：默认下载（保持图文完整）

**必须问的两种情况**：

1. **知识库存哪里没定** → 问清楚路径，别猜
2. **目标号有多个同名** → 让用户给一篇文章链接确认

**不要问的**：期限（全量抓取不需要期限）、要不要图片（默认要）、并发数（用默认）。

---

## 参考资料

按需读取，别一次全加载：

| 文件 | 什么时候读 |
|---|---|
| `references/setup.md` | 首次安装、环境报错时 |
| `references/troubleshooting.md` | 抓取失败、风控、报错码时 |
| `references/platform-change.md` | 用户问「为什么网上工具都不能用了」时 |
| `references/alternatives.md` | 用户问「还有没有别的办法」时 |

---

## 快速自检

环境是否就绪，一条命令：

```bash
python3 {baseDir}/scripts/selftest.py
```

期望输出 `✅ 通过 34   ❌ 失败 0`。

# 微信公众号文章归档

给一个公众号，把它发过的文章全下载下来，存成 Markdown 放你自己电脑里。

抓完是一个文件夹，里面每篇文章一个 md 文件，图片也一起下好。
丢进 Obsidian 就能读、能搜、能双链。

## 什么时候用得上

想把某个博主几年的文章存下来慢慢看。

想摸清一个号是怎么做内容的 —— 它写过什么、怎么起标题、哪类选题反复出现。

有些文章怕哪天被删了，先存一份。

或者就是单纯的：这个号写得好，我想把它攒起来。

也可以不抓全部 —— 只要最近 1 个月、3 个月、半年或 1 年的，都行。

## 为什么现在得专门做个工具

2026 年 7 月 30 日，微信把「公众号后台查别人文章列表」这个接口关了。

网上那些导出工具基本都是靠这个接口活的，所以那天一起失效了。
换一个工具也是一样的结果，因为它们用的是同一个口子。

这个工具绕开了 —— 走微信读书。微信读书把公众号当书来管理，
和公众号后台是两回事，所以那边关了这边还能用。

现在能翻到「这个号第一篇」的做法，我目前只找到这一条。

## 怎么用

### 先做一次准备

需要一个单独的 Chrome 窗口，登录一次微信读书：

```bash
bash ~/.weread-mp-fetcher/start-chrome.sh
```

窗口弹出来后扫码登录。**就这一次**，之后不用再扫。
（这个 Chrome 跟你平时用的不是同一个，平时那个不用关。）

### 然后抓

两种抓法，看你要什么。

**要全部**，从今天一路翻到这个号发过的第一篇：

```bash
bash scripts/gzh.sh crawl --from-url "https://mp.weixin.qq.com/s/xxxxx"
```

也可以直接给公众号名字：

```bash
bash scripts/gzh.sh crawl --from-name "某某研究院"
```

**只要最近一段**，用 `weread` 加个期限：

```bash
bash scripts/gzh.sh weread --from-name "某某研究院" --period 1m   # 最近 1 个月
bash scripts/gzh.sh weread --from-name "某某研究院" --period 3m   # 最近 3 个月
bash scripts/gzh.sh weread --from-name "某某研究院" --period 6m   # 最近半年
bash scripts/gzh.sh weread --from-name "某某研究院" --period 1y   # 最近 1 年
```

期限五个：`1m` `3m` `6m` `1y` `all`（不写就是 all）。

它不会傻抓固定几页 —— 而是**一直往前翻，翻到覆盖住这个期限起点为止**。
要 3 个月就翻到 3 个月前停下，不多翻；要 1 年就翻满 1 年。

万一本号文章太多、翻到页数上限还没到期限起点，它会**明确告诉你没抓够**，
而不是少给几个月还装作没事：

```bash
# 想翻得更深就加大页数上限（默认 20 页）
bash scripts/gzh.sh weread --from-name "某某研究院" --period 1y --pages 40
```

> **两种抓法的区别**：`crawl` 为「全量」设计 —— 能断点续跑、能补漏；
> `weread` 为「取一段」设计 —— 翻到期限就停，更省时间。
> 两个都支持 `--from-url` / `--from-name` / `--book-id` 三种定位方式。

**不用守着。** 断了、关了、电脑睡了都无所谓，
再跑一次同样的命令，它从上次停的地方接着走（`crawl` 支持续跑）。

### 最后补一下

```bash
bash scripts/gzh.sh backfill --account "某某研究院"
```

这一步是查漏。有时候会出现「文章列出来了但没存下来」的情况，
跑一下这条能补齐。

### 抓完长什么样

```
某某研究院/
├── articles/     每篇文章一个 md
├── images/       文章里的图
├── index.md      全部文章的目录
└── manifest.json 这次抓取的记录
```

随便点开一篇，开头是这样：

```markdown
---
title: "文章标题"
account: "某某研究院"
author: "作者名"
publish_date: "2026-01-15"
source_url: "https://mp.weixin.qq.com/s/..."
---

# 文章标题

> **公众号**：某某研究院 ｜ **作者**：某某 ｜ **发布时间**：2026-01-15 08:16

![图片](../images/xxx.png)

正文……
```

图片用的是相对路径，在 Obsidian 里能直接显示。

## 安装

```bash
# 1. 下载
mkdir -p ~/.agents/skills
git clone https://github.com/renzheng0415/renrenskill-gzh-archive.git \
  ~/.agents/skills/renrenskill-gzh-archive

# 2. 软链到你的 AI 客户端（以 Claude 为例，别的同理）
ln -sfn ~/.agents/skills/renrenskill-gzh-archive \
  ~/.claude/skills/renrenskill-gzh-archive

# 3. 装抓文章列表的工具
mkdir -p ~/.weread-mp-fetcher && cd ~/.weread-mp-fetcher
git clone --depth 1 https://github.com/Pengyf04/weread-mp-fetcher.git tool
```

电脑上要有 Python 3.9+、Node.js 18+、Google Chrome。
Python 的依赖第一次运行时会自动装。

装完跑一下看环境对不对：

```bash
python3 scripts/selftest.py
```

更多配置说明在 [`references/setup.md`](references/setup.md)。

## 三件得先知道的事

**那个 Chrome 要开着。**

微信最近在改文章页的加载方式，改完之后只有用浏览器才读得到正文。
所以抓的时候窗口得留着。不用的时候关掉没关系，不影响你平时用的 Chrome。

**它故意跑得慢。**

两个请求之间要等 25 秒。一小时最多 40 次，一天最多 200 次。

不是写错了，是必须这么慢。调快会被微信读书拦，
拦了要等十几分钟才能恢复，反而更慢。

这个限制写死在程序里了，绕不过去 —— 包括我自己临时想测一下也不行。

看还剩多少额度、现在能不能抓：

```bash
bash scripts/gzh.sh pace
```

它会直接告诉你「今天还能发几个」「现在能不能发」。
**别去看 `weread-mp-fetcher` 自带的 `--quota`** —— 本工具抓列表走自己的通道，
不经过它，那边永远显示 0。以 `pace` 为准。

**文章版权是原作者的。**

自己存着看、做研究没问题。想转载、发出去、拿去卖，得按原作者的要求来。

## 抓不动了怎么办

先看这几条：

连不上 Chrome → 跑 `bash ~/.weread-mp-fetcher/start-chrome.sh`

提示触发风控 → 等 15 到 30 分钟再说，别一直重试

提示登录失效 → 在 Chrome 窗口里重新扫一次码

有几篇抓不到 → 正常的。可能是作者设了隐私，或者那篇只有视频没文字

数量对不上 → 跑一次 `backfill`

详细的排错手册：[`references/troubleshooting.md`](references/troubleshooting.md)

## 也可以让 AI 用

这个仓库本身就是一个 Agent Skill，`SKILL.md` 是写给 AI 看的。

装好之后你可以直接说：

> 把某某研究院的历史文章全抓下来

AI 会自己看说明书、检查环境、跑抓取、最后验收。

## 文件都在哪

```
.
├── SKILL.md                 写给 AI 看的说明书
├── README.md                你正在看的
├── LICENSE                  MIT
├── THIRD_PARTY_NOTICES.md   用到的第三方东西和版权说明
├── references/              需要时再查
│   ├── setup.md             装和配置
│   ├── troubleshooting.md   出问题查这里
│   ├── platform-change.md   微信关接口那件事的来龙去脉
│   └── alternatives.md      我还试过哪些别的办法
└── scripts/                 程序
    ├── gzh.sh               所有命令的入口
    ├── gzh/                 Python 主程序
    ├── tools/               Node 脚本（跟浏览器打交道）
    ├── tests/               测试
    └── selftest.py          环境自检
```

## 命令都在这里

```bash
GZH="bash scripts/gzh.sh"

$GZH crawl --from-name "某某号"        # 抓全部，能续跑
$GZH weread --from-name "某某号" --period 3m   # 只抓最近 3 个月
$GZH backfill --account "某某号"       # 补漏
$GZH pace                          # 看现在能不能抓
$GZH article "<文章链接>"           # 只抓一篇
$GZH urls links.txt                # 抓一批链接
$GZH rebuild --account "某某号"     # 从已下载的文件重建目录
```

## 许可

MIT。第三方依赖和版权说明见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

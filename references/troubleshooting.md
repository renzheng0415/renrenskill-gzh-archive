# 排错手册

抓取出问题时按现象查。**先看错误码，再看现象。**

---

## 错误码速查

| 返回 | 含义 | 处理 |
|---|---|---|
| `-2041` | 页面上下文校验失败 **或** 触发风控 | 等 20-30 分钟自动恢复；别连发请求 |
| `-2010` | 微信读书登录失效 | 在专用 Chrome 里重新扫码 |
| `-2012` | **短时风控**（最常见）或该号未收录 | 停手等 15-30 分钟；别当成登录失效 |
| `200013` | 公众号后台接口已封禁 | **不用管** —— 本 Skill 不走这条路 |
| 探针报 `captcha` | 弹了验证码 | 手动过一下再重跑 |
| 探针报 `blank` | 触发风控 | 停手，隔几小时再试 |

---

## `-2012` 怎么判：风控 还是 真的没收录

**先记住：「书架接口也失败」不代表登录失效。** 见下方「一个必须避开的误判」。

| 观察 | 结论 |
|---|---|
| 所有接口（含书架）都 `-2012`，等十几分钟自愈 | **短时风控** ← 最常见 |
| 只有某一个号 `-2012`，别的号正常 | 该号未被微信读书收录 |
| 持续 `-2012` 且**很久**不自愈 | 才考虑登录问题（此时会伴随 `-2010`） |

**处理**：`-2012` 九成是风控 → **停手等 15-30 分钟**，别连发请求。
只有确认是登录问题（拿到 `-2010` 且久不恢复）才需要重新扫码。

---

### ⚠️ 一个必须避开的误判（我真踩过）

**错误做法：用 `document.cookie` 判断登录状态。**

```js
// ❌ 这样写一定得出错误结论
JSON.stringify(document.cookie.split(';').map(c => c.split('=')[0]))
```

**为什么错**：`wr_skey` / `wr_vid` / `wr_rt` 都是 **HttpOnly** 的 ——
JS **永远读不到**，但浏览器发请求时会自动带上。
用这个方法会看到「`wr_skey` 不见了」，从而**误判成登录失效**，
然后让用户白白重新扫码。

**正确做法**：通过 CDP 读全部 cookie（含 HttpOnly）：

```bash
# 在专用 Chrome 上执行
node -e "
const r = await (await fetch('http://127.0.0.1:9333/json/version')).json();
const ws = new WebSocket(r.webSocketDebuggerUrl);
ws.onopen = () => ws.send(JSON.stringify({id:1, method:'Network.getAllCookies'}));
ws.onmessage = e => {
  const d = JSON.parse(e.data);
  if (d.id === 1) {
    const wr = (d.result.cookies||[]).filter(c => c.domain.includes('weread'));
    ['wr_skey','wr_vid','wr_rt'].forEach(k => {
      const f = wr.find(c => c.name === k);
      console.log(k, f ? '✅ 存在 (HttpOnly)' : '❌ 无');
    });
    ws.close();
  }
};
"
```

**判据**：`Network.getAllCookies` 里 `wr_skey` 和 `wr_vid` 存在 → 登录是好的。

---

### 另一个迷惑现象

**页面仍显示公众号名**（如「某某号 - 公众号 - 微信读书」）用的是本地缓存，
既不能证明登录有效，也不能证明失效。**别拿它当判据。**

真正可靠的判据只有一个：**直接发一次真实请求看返回码**。

---

## 关键判据：怎么区分「风控」和「真失败」

**风控的特征：连 `offset=0` 都失败。**

```bash
# 基准测试：offset=0 是永远应该有数据的
bash {baseDir}/scripts/gzh.sh weread --probe
```

| 观察 | 结论 |
|---|---|
| offset=0 正常，只有大 offset 失败 | 可能是**深度边界**（正常到底） |
| **offset=0 也失败** | **风控**，等冷却 |
| 所有号都失败 | **风控**（不是某个号的问题） |
| 只有某个号失败，别的正常 | 该号在微信读书无收录 |

> **踩过的坑**：曾因为连续发请求触发风控，看到「offset=200 可以、400 失败」，
> 就误判成「平台在 200-400 之间有硬上限」。**错了** —— 那是限流扩散。
> 真上限不会让 offset=0 一起失效。

---

## 现象 → 处理

### 「连不上专用 Chrome（端口 9333）」

```bash
# 1. 端口是否在监听
lsof -nP -iTCP:9333 -sTCP:LISTEN

# 2. 是否真的是 CDP
curl -s http://127.0.0.1:9333/json/version
```

- 端口没监听 → 跑 `start-chrome.sh`
- 返回 404 → `--user-data-dir` 没生效（Chrome 136+ 会静默忽略端口参数）
- 端口被别的程序占 → 改 config 里的 `chromePort` 和启动脚本的端口

### 「没有找到微信读书阅读器页」

需要一个 `/web/mp/reader/...` 的标签页作为请求上下文。

**修复**：在专用 Chrome 里打开任意公众号的阅读页，或重跑 `start-chrome.sh`。

### 「等待正文渲染超时」

browser 通道渲染不出正文。可能原因：

| 页面文本特征 | 含义 |
|---|---|
| 「根据作者隐私设置，无法查看该内容」 | 作者设了隐私，**抓不到，正常** |
| 「环境异常」「去验证」 | 需要过验证码 |
| 空白/只有导航 | 页面没加载好，重试一次 |

### 「正文为空」（direct 和 api 都失败）

这是 **JS 动态渲染页**。检查 browser 通道是否可用：

```bash
# Chrome 在跑吗
curl -s http://127.0.0.1:9333/json/version >/dev/null && echo ok

# 单篇测试
bash {baseDir}/scripts/gzh.sh article "<那篇文章的链接>"
```

若 browser 可用仍失败 → 页面结构又变了，需更新 `scripts/tools/render_article.mjs`。

### 数量对不上：文件数 ≠ 索引数

**用 rebuild 修**：

```bash
bash {baseDir}/scripts/gzh.sh rebuild --kb "<知识库>" --account "<公众号名>"
```

原因：索引原本只在导出流程**结束时**写一次，中途中断会留下旧索引。

### 列表见过 N 篇，知识库只有 M 篇（N > M）

**用 backfill 补**：

```bash
bash {baseDir}/scripts/gzh.sh backfill --account "<公众号名>" --kb "<知识库>"
```

**根因**（已修复但差集仍可能存在）：
断点文件里的 `seen` 记的是「URL 在列表里出现过」，
知识库文件是「正文已成功抓下」。中断过的号会留下差集，
而续跑从更晚的 offset 开始，**那批永远不会被回头扫到**。

> **实测踩到过**：`seen` 118 篇，知识库 100 篇，静默丢了 18 篇。

**backfill 只读断点文件，不再请求列表接口**，所以不消耗微信读书额度。

---

## 风控恢复时间

实测：**约 25 分钟**自动恢复。

工具会自动等 15 分钟重试（单次运行最多 2 次）。若两次都不行，本次放弃，
**进度已保存**，隔几小时再跑即可。

---

## 终极大法

抓取状态全在磁盘上，**没有任何东西只存在内存里**。所以：

```bash
# 1. 停掉一切
pkill -f "gzh crawl"

# 2. 重建索引（从文件反推）
bash {baseDir}/scripts/gzh.sh rebuild --kb "<知识库>"

# 3. 补齐差集（从断点文件反推）
bash {baseDir}/scripts/gzh.sh backfill --account "<公众号名>" --kb "<知识库>"

# 4. 继续抓
bash {baseDir}/scripts/gzh.sh crawl --from-url "<链接>" --interval 30
```

**断点状态是原子写入的**（先写 `.tmp` 再 rename），被 kill 不会留下半个文件。

---

## 不要做的事

| ❌ 别做 | 为什么 |
|---|---|
| 用脚本裸调 CDP 接口「测深度」 | 实测这么做 18 秒发 7 个请求直接触发风控 |
| 缩短 `--interval` 到 10 秒以下 | 工具作者实测 30+ 请求/天就触发过反爬 |
| 风控后反复重试 | 会延长封禁 |
| 新开标签页重试 | 实测「反复新开标签页」正是触发风控的模式 |
| 删掉 `.weread-crawl.json` 重来 | 会丢失断点，前面白抓（除非真要 `--reset`） |

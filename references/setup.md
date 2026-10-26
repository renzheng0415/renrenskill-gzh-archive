# 安装与前置配置

首次使用本 Skill 时读这份。已装好且能跑通的，跳到文末「快速自检」。

---

## 依赖总览

| 依赖 | 用途 | 必需 |
|---|---|---|
| Python 3.9+ | 主程序 | ✅ |
| `requests` | HTTP 请求 | ✅（包装器会自动装） |
| Node.js 22+ | 浏览器渲染通道 | ✅ |
| Google Chrome | 专用实例，微信读书 + JS 渲染页 | ✅ |
| 微信读书账号 | 文章列表数据源 | ✅ |

**不需要**：公众号后台、auth-key、Docker、服务器。

---

## 第一步：装微信读书抓取工具

这个工具负责「列出某公众号的全部文章」。

```bash
mkdir -p ~/.weread-mp-fetcher
cd ~/.weread-mp-fetcher
git clone --depth 1 https://github.com/Pengyf04/weread-mp-fetcher.git tool
```

Node 脚本使用内置 fetch 和 WebSocket，需要 Node.js 22+，不用 `npm install`。

**然后写配置** `~/.weread-mp-fetcher/tool/config.json`：

```json
{
  "accounts": [],
  "readerUrl": "",
  "maxRunsPerDay": 2,
  "requestIntervalMs": 3000,
  "maxRequestsPerDay": 40,
  "maxPagesPerRun": 3,
  "chromePort": 9333,
  "chromeProfileDir": "~/.weread-mp-fetcher/chrome-profile",
  "outDir": "~/.weread-mp-fetcher/out",
  "statePath": "~/.weread-mp-fetcher/quota.json"
}
```

> ⚠️ **`chromeProfileDir` 必须填，`chromePort` 必须与启动脚本一致。**
> Chrome 136 起，只给 `--remote-debugging-port` 不给 `--user-data-dir` 时，
> 端口参数会被**静默忽略**（不报错）。这是最常见的「明明启动了却连不上」原因。

---

## 第二步：启动专用 Chrome

```bash
bash ~/.weread-mp-fetcher/start-chrome.sh
```

如果这个脚本不存在，手动启动：

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9333 \
  --user-data-dir="$HOME/.weread-mp-fetcher/chrome-profile" \
  --no-first-run --no-default-browser-check \
  "https://weread.qq.com/"
```

**这个实例的特点：**
- 独立 `user-data-dir`，与日常 Chrome **互不影响**（不用关掉日常那个）
- 只监听 `127.0.0.1`，外部访问不到
- 登录态存在专用目录里，Chrome 完全退出后仍保持登录

**在弹出的窗口里扫码登录微信读书。遇到验证码或登录过期时仍需人工处理。**

---

## 第三步：验证

```bash
export GZH="bash <skill目录>/scripts/gzh.sh"

# 1. Chrome 调试端口通不通
curl -s http://127.0.0.1:9333/json/version | head -c 120

# 2. 微信读书登录态
$GZH weread --shelf

# 3. 端到端（最能说明问题）
$GZH article "https://mp.weixin.qq.com/s/hNtnQsJ_T7GIdBQ5-LUoMQ"
```

第 3 步成功 = 三条通道里至少一条通了，可以开始归档。

---

## 配置知识库根目录

`~/.config/gzh/config.json`：

```json
{
  "kb_root": "01_内容生产HQ/公众号素材库",
  "auth_key": ""
}
```

- `kb_root` **相对路径按当前工作目录解析** —— 建议写绝对路径避免歧义
- `auth_key` 是旧路径（公众号后台）用的，**现在不需要了**（那条路已封）

也可以用 `--kb` 每次显式指定，优先级更高。

---

## 常见环境问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `连不上专用 Chrome（端口 9333）` | Chrome 没开，或端口不对 | 跑 `start-chrome.sh`；确认 config 里 `chromePort` 一致 |
| 端口开着但 `curl` 返回 404 | `--user-data-dir` 没生效（默认目录被忽略端口） | 必须带 `--user-data-dir`，且指向专用目录 |
| `-2010 用户不存在` | 微信读书登录态失效 | 在专用 Chrome 里重新扫码 |
| `-2041` | 触发风控 | 等 20-30 分钟自动恢复，**别连发请求** |
| `找不到 node` | 没装 Node.js | 装 Node 18+；或加 `--no-browser` 跳过渲染通道 |
| `缺少 requests` | Python 没装依赖 | 包装器会自动装；失败则手动 `pip install requests` |

---

## 快速自检

```bash
python3 <skill目录>/scripts/selftest.py
```

期望：`✅ 通过 34   ❌ 失败 0`

**注意**：自检里有一条会访问第三方服务，若外网抖动会显示「跳过」——
那是噪音不是故障，不影响代码正确性。

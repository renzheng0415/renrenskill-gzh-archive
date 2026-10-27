---
name: renrenskill-gzh-archive
display_name: renrenskill公众号归档
display_name_en: renrenskill WeChat Account Archiver
description: 把微信公众号历史文章归档为本地 Markdown 知识库，保存原文链接、本地图片和索引。支持微信读书列表、新版 WR1 正文标识、图文相册、断点续跑、持续运行、状态查询及完整性验收。用户要求抓公众号、导出历史文章、备份公众号或建立公众号资料库时使用。
description_zh: 把微信公众号历史文章归档为本地 Markdown 知识库，保存原文链接、本地图片和索引。支持微信读书列表、图文相册、断点续跑和完整性验收。
description_en: Archive WeChat Official Account articles into a local Markdown knowledge base with source links, images, and an index. Supports WeChat Reading lists, image albums, resumable crawls, and completeness checks.
version: 1.1.2
---

# 公众号文章归档（renrenskill）

目标是尽可能完整保存目标公众号在数据源中可访问的文章。接口变化、账号收录范围、登录、验证码、作者权限和风控都可能限制结果。不能承诺固定耗时、零风控或一定追溯到开号第一篇。

## 路径和输入

`{baseDir}` 为本 SKILL.md 所在目录，调用时展开为真实绝对路径。所有业务命令走：

```bash
bash "{baseDir}/scripts/gzh.sh" --help
```

用户给了知识库路径就使用该路径，否则读取 `~/.config/gzh/config.json` 的 `kb_root`。相对路径按当前工作目录解析，执行时转换成绝对路径。确实没有路径时才询问。

目标优先用一篇该号文章链接定位。名称搜索需要有效的导出服务认证，认证失败时查找公开文章链接或请用户提供；不要把名称搜索认证失效当成微信读书登录失效。同名账号不能自行猜选。

## 前置检查

先确认本机时间、Chrome 调试端口和阅读器状态：

```bash
date
curl -s -m 3 http://127.0.0.1:9333/json/version
bash "{baseDir}/scripts/gzh.sh" weread --probe
bash "{baseDir}/scripts/gzh.sh" pace
```

缺少环境时读 [安装说明](references/setup.md)。Chrome 必须使用独立数据目录；登录或验证码需要用户手动完成。看到验证码节点不等于它仍可见，不能只凭节点存在或 `document.cookie` 推断状态。

登录失效、验证码、页面上下文异常和风控分开报告。`-2041` 既可能来自页面校验，也可能来自风控；持续失败时使用现有阅读器状态诊断，不反复新开标签或请求其他号试探。见 [排错](references/troubleshooting.md)。

## 主流程

1. 核对目标号和保存路径。
2. 抓一篇实际目标文章验证正文、原文链接、元数据和图片。
3. 全量请求使用持续运行入口；它每批保存断点，等待额度恢复，单篇失败保留记录并继续。
4. 启动后确认进程存在且有真实进度，不把“已启动”说成“已完成”。
5. 列表到底后补抓差集，再检查验收结果。

```bash
bash "{baseDir}/scripts/gzh.sh" crawl \
  --from-url "https://mp.weixin.qq.com/s/xxxxx" \
  --account "公众号名" --interval 25 --continuous --kb "绝对知识库路径"
```

后台运行时保存日志并保留 PID。例如：

```bash
nohup bash "{baseDir}/scripts/gzh.sh" crawl \
  --book-id MP_WXS_1234567890 --account "公众号名" \
  --continuous --kb "绝对知识库路径" > "绝对日志路径" 2>&1 < /dev/null &
```

必须保持 Chrome 打开、电脑不休眠。进程不跨系统重启自动恢复；退出后重跑相同命令接续。`crawl` 不加 `--continuous` 时仍是有限页数批次，不能称为无人值守持续任务。不要同时启动两个写入同一账号目录的任务。

持续模式保存 `.gzh-status.json`、`.gzh-heartbeat` 和进程锁。普通异常采用逐步延长的等待，登录失效停止等待人工处理；分页重复或停滞列为待检查，不能据此声称已经到底。

```bash
bash "{baseDir}/scripts/gzh.sh" status --account "公众号名" --kb "知识库路径"
bash "{baseDir}/scripts/gzh.sh" audit --account "公众号名" --kb "知识库路径"
bash "{baseDir}/scripts/gzh.sh" backfill --account "公众号名" --kb "知识库路径"
```

## 内容与限频

- 普通公开文章优先直接读取；需要时使用原有服务或浏览器兜底。
- 微信读书新版 `WR1…` 标识不是微信短链接。列表保留 reviewId，通过 `/web/mp/content` 获取 HTML，再读取真实原文链接。
- 图文相册提取配文与原始图片，不把水印缩略图当原图。视频无可提取正文时记录未支持或失败，不生成成功占位文章。
- 列表和微信读书正文请求共享账本，默认间隔至少 25 秒，每小时最多 40 次，每 24 小时最多 200 次。正文改走微信读书会消耗额度，不能沿用旧的整号耗时估计。
- 风控错误记账并进入冷却；额度耗尽不是新一次风控。不可绕过账本或调快测试。

## 完成与进度口径

始终区分：已发现文章、已成功保存的列表文章、目录总文章（可能含额外单篇）、待补抓、列表是否已到底、图片是否完整。

只有空页和更远 offset 都确认无群发，才标记列表结束。断点在慢速下载之前写入，避免中断丢失当前页。失败文章与真实原文链接映射必须跨进程保留。

`status` / `audit` 输出：

- `list_finished`：是否确认历史列表末尾；
- `total_known`：当前是否能确定数据源列表总数；未到底只能报告已发现数量；
- `archived_from_list`、`files`、`unresolved`：列表成功数、目录文件数及差集；
- `missing_images`、`remote_images`：缺失图片和未本地化图片；
- `complete`：列表结束且正文、图片均通过检查。

只有 `complete=true` 才能报告该数据源范围内归档完整。`finished` 或退出码单独不能证明全部正文成功。检查月份分布可辅助发现缺口，但某月无文章不等于采集遗漏。

用户问“还在抓吗”时，现场检查进程、心跳、最近成功和下次重试时间，明确回答正在下载、限流等待、需要登录、失败或完成。总数未知时不要估造总数；速度只在正常抓取区间估算，等待和故障另计。

## 交付目录

```text
公众号名/
├── articles/             Markdown 与原文来源
├── images/               本地图片
├── index.md              阅读索引
├── .gzh-index.json       结构化索引
├── .weread-crawl.json    列表断点、原文映射
├── .gzh-status.json      运行状态及下次动作
├── manifest.json         文件清单与失败记录
└── audit.json            持续模式结束后的验收
```

## 其他命令

```bash
bash "{baseDir}/scripts/gzh.sh" article "文章链接" --kb "知识库路径"
bash "{baseDir}/scripts/gzh.sh" urls links.txt --kb "知识库路径"
bash "{baseDir}/scripts/gzh.sh" rebuild --account "公众号名" --kb "知识库路径"
bash "{baseDir}/scripts/gzh.sh" weread --shelf
bash "{baseDir}/scripts/gzh.sh" weread --from-url "文章链接" --period 6m --pages 20
python3 "{baseDir}/scripts/selftest.py"
python3 -m unittest discover -s "{baseDir}/scripts/tests" -q
```

按需读取 [安装](references/setup.md)、[排错](references/troubleshooting.md)、[历史平台变化记录](references/platform-change.md)、[其他路径](references/alternatives.md)。历史实测数据不等于当前服务保证。

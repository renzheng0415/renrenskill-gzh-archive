# 微信公众号文章归档

`renrenskill-gzh-archive` 把微信公众号可访问的历史文章保存为 Markdown、本地图片和阅读索引，适合放入 Obsidian。

支持普通文章、图文相册、微信读书新版 WR1 文章标识、断点续跑、持续运行、状态查询和完整性检查。Skill 名称保持不变。

## 安装与环境

```bash
npx -y skills add renzheng0415/renrenskill-gzh-archive -g --all
```

也可将仓库检出到 Agent 的 skills 目录。需要 Python 3.9+、requests、Node.js 22+（内置 fetch/WebSocket）、Google Chrome，持续模式目前适用于支持 `fcntl` 的 macOS/Linux。

微信读书使用单独的 Chrome 数据目录，用户在该窗口扫码登录并处理验证码。安装现有微信读书辅助工具和启动方式见 [setup.md](references/setup.md)。

```bash
bash ~/.weread-mp-fetcher/start-chrome.sh
bash scripts/gzh.sh weread --probe
```

登录可能过期。抓取期间需要 Chrome 开着、电脑不休眠。

## 归档

推荐用一篇目标公众号文章定位账号：

```bash
bash scripts/gzh.sh crawl --from-url "https://mp.weixin.qq.com/s/xxxxx" \
  --account "公众号名" --kb "/absolute/path/to/library" --continuous
```

`--continuous` 每批保存断点，等待额度恢复，并对临时失败进行延长间隔的重试。登录失效会停下提示；分页停滞进入待检查状态。该选项不会替你安装系统启动服务，电脑重启或进程终止后，重跑相同命令续接。

不加 `--continuous` 是有限批次：默认每次最多 5 页。按名称搜索需要额外导出服务认证；认证不可用时改用文章链接，不必重新登录微信读书。

后台运行可用 `nohup`，请将标准输出和错误写入你指定的日志文件。启动后使用下面命令确认真实运行状态：

```bash
bash scripts/gzh.sh status --account "公众号名" --kb "/absolute/path/to/library"
bash scripts/gzh.sh audit --account "公众号名" --kb "/absolute/path/to/library"
bash scripts/gzh.sh backfill --account "公众号名" --kb "/absolute/path/to/library"
```

## 完成标准

`list_finished` 表示列表已确认到末尾；`complete` 还要求所有发现的正文已保存、图片存在且已本地化。目录中额外导入的单篇文章单独计入 `files`，不会混成列表成功数。

未翻完列表时总篇数未知。不会以文件数、进程存在或一页为空来宣称全量完成。失败原因与未保存文章保留供补抓。持续模式结束时写入 `audit.json`。

## 输出

```text
公众号名/
├── articles/             每篇一个 Markdown，含原文链接
├── images/               本地图片
├── index.md              文章索引
├── .gzh-index.json       结构化索引
├── .weread-crawl.json    列表断点与真实链接映射
├── .gzh-status.json      当前状态、PID、下一次重试时间
├── .gzh-heartbeat        持续进程心跳
├── manifest.json         文件清单
└── audit.json            结束验收
```

## 请求节奏与限制

默认两个微信读书请求间隔至少 25 秒，每小时最多 40 次，每 24 小时最多 200 次。多个进程通过同一账本锁串行取得请求许可。新版正文接口也计入额度，图片下载另行处理。

`-2041` 可能是阅读器上下文校验或风控，不能一律归因登录或限流。连续失败会延长等待；不会通过改快频率、换号试探来绕过。详情见 [troubleshooting.md](references/troubleshooting.md)。

```bash
bash scripts/gzh.sh pace
```

是否收录全部历史由微信读书决定；作者删除、隐私限制、验证码、接口变化或暂未支持的页面类型都可能造成缺失。没有固定耗时、零风控或覆盖所有公众号的保证。原作者的版权与使用要求仍适用。

## 其他操作

```bash
bash scripts/gzh.sh article "文章链接" --kb "/absolute/path/to/library"
bash scripts/gzh.sh urls links.txt --kb "/absolute/path/to/library"
bash scripts/gzh.sh rebuild --account "公众号名" --kb "/absolute/path/to/library"
bash scripts/gzh.sh weread --from-url "文章链接" --period 6m --pages 20
bash scripts/gzh.sh weread --shelf
```

`weread` 用于按期限取得有限数据；`crawl` 用于可恢复的历史归档。

## 验证

```bash
python3 -m unittest discover -s scripts/tests -q
python3 scripts/selftest.py
```

测试覆盖原有解析与限流，以及相册、末尾复核、下载前保存断点、额度处理和完整性口径。离线测试通过不代表当前线上接口已解除风控；真实验证结果须另行说明。

MIT。第三方说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

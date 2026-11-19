# eagle_eye 架构

## 目标

扩大 Agent 的搜索面/信息面：Agent 只学一套命令（`eagle channels` / `eagle search`）和一套数据模型（Query → Item），
背后接多少渠道、各用什么语言实现，对 Agent 透明。

## 分层

```
Agent ──(Skill 指引: skills/eagle-search)──► eagle（PATH 上；开发模式 bin/eagle）(eagle_eye/cli.py)
                           │
                 ┌─────────┴──────────┐
        eagle channels / search     eagle <渠道> ...（原生子命令：x → channels/x/cli.py；xhs → native_command 透传）
                           │
                 core/registry.py  ── 读 eagle.toml，按 type 构造 Channel（失败 → BrokenChannel=unavailable）
                           │
                 core/router.py    ── ThreadPool 并发 fan-out → 每渠道 SearchResult 或 ChannelError
                           │          → merge(sort) → 跨渠道 URL 去重 → limit → AggregateResult
                 core/output.py    ── JSON / Markdown
                           │
        ┌──────────────────┴───────────────────┐
  进程内渠道（Python 类）                   外部渠道（任意语言）
  channels/x/adapter.py: XChannel          channels/external.py: ExternalChannel
    └ xsearch 原有 pipeline/api/tws           └ 子进程: <command...> describe|search
                                                  stdin Query JSON → stdout SearchResponse JSON
                                                  └ bin/eagle-xhs channel ...（backends/xhs，Go）
```

## 关键决定

- **Python 核心 + 多语言渠道**：核心只用标准库；外部渠道走 JSON-over-stdio（`docs/channel-protocol.md`），
  进程隔离意味着渠道崩溃、卡死、依赖缺失都只影响它自己（超时杀进程组、坏 JSON → internal 并附 stderr 尾部）。
- **统一 CLI + Skill，暂不做 MCP**：CLI 对任何 Agent / 人 / cron 都可用；以后要做 MCP 只需在 core 之上加一层。
  Skill 遵循开放标准 Agent Skills，与具体 Agent 无关；源文件放仓库根 `skills/`（不放在任何 Agent 会自动加载的目录下，如 `.agents/`、`.claude/`），
  由用户按需接入：`eagle skill install`（默认 `~/.agents/skills`，`--agent claude` → `~/.claude/skills`，其他工具 `--target DIR`），
  不支持 Skill 的 Agent 用 `eagle skill show` 把正文放进其指令文件，或直接运行 `eagle --help` / `eagle channels` 自行发现。
- **错误即数据**：7 个错误码直接告诉 Agent 下一步（登录 / 等待 / 换渠道 / 修参数），多渠道时汇总在 `errors` 里，
  单渠道失败不影响整体；全部失败才 exit 1。
- **合并策略**：`relevance` 时各渠道相关度不可比，按渠道顺序轮流交错；`time` / `score` 全局排序，缺值排最后。
  去重键是归一化 URL（忽略协议、www、twitter.com→x.com、结尾斜杠、fragment、`xsec_token`/`utm_*` 等易变参数）加 (渠道, id)。
- **私有参数**：`--opt k=v` 给所有渠道，`--opt <渠道>.k=v` 只给该渠道；渠道忽略未知键并在 `meta.ignored` 说明。
- **兼容**：X 渠道沿用 `~/.config/xsearch/twscrape.db`（存在时；否则 `$EAGLE_HOME/x/twscrape.db`）和 `XSEARCH_*` 环境变量（`EAGLE_X_*` 优先），
  cookie 接收服务协议不变，已装的浏览器扩展继续可用（扩展源码在 `extensions/x-cookie/`，manifest 名与 /ping 的 app 名仍是 "xsearch"）。
- **浏览器扩展放顶层 `extensions/`**：按 `<渠道>-<用途>` 命名；源码只此一份，构建 wheel 时打进包内 `eagle_eye/_bundled/`（Skill 同理），
  `eagle x extension` 打印其目录；浏览器不能直接加载该目录时（如 WSL 里给 Windows 浏览器用），用 `eagle x extension --copy-to DIR` 复制副本。
- **分发**：`uv tool install` 安装 + `eagle setup` 下载小红书二进制；用户数据在 EAGLE_HOME。约定见 [distribution.md](distribution.md)。

## X 渠道的 Query 映射

| Query | X |
|---|---|
| text | X 高级搜索语法原样透传，末尾追加 `options.suffix`（默认 `-is:retweet`） |
| since_hours | `since_time:<unix>`；null/0 不限 |
| sort | relevance → `Top`、保持 X 顺序；time → `Latest` + 按时间；score → `Latest` + 按 赞+2×转推 |
| limit | 本地截断 |
| options | pages / type / articles / keywords / exclude / min_score / suffix / backend |

Item 映射：retweets → reposts，replies → comments，score = 赞 + 2×转推，followers / isReply / article / hits 放 `extra`；
抓了长文时 `title` = 长文标题。

限速：twscrape 按 X 返回的 `x-rate-limit-remaining/reset` 按账号、按接口自动锁定并等待（15 分钟窗口），具体次数以 X 为准；
twitterapi.io 免费档 0.2 QPS（未充值的新账号）。`TWS_INTERVAL` / `TWS_WAIT` / `INTERVAL` 是本项目客户端参数，不是平台限额。

## 小红书渠道

由 `backends/xhs`（Go，ee-xhs 维护）实现协议的 `channel describe|search`；核心通过 `eagle.toml` 登记，不写 Python。
默认 `browser.headless: true`：每次搜索用专用 profile 自己启动无头 Chrome、用完关闭；`headless: false` 才接管 `cdp_url` 上的 Chrome（`eagle xhs browser` 启动，也用于调试）。
原生命令是 `eagle xhs login`（有界面窗口扫码，与搜索共用专用 profile）/ `eagle xhs browser` 等（原样透传给 eagle-xhs）；`login` 与渠道搜索（以及并发的两次搜索）共用一把浏览器锁，因此会返回 `busy`；触发风控验证后熔断冷却，因此会返回 `cooldown`（不限搜索次数，不返回 `rate_limited`）。
搜索为快速模式：直达搜索结果页 URL，详情页多标签并行读取（`channel.detail_concurrency` 默认 3），不模拟真人节奏，风控/封号风险更高；耗时估计见 README「渠道代价」（未实测）。

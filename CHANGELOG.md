# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-11-19

首个版本。

### 新增

- **统一信息搜索中间层**：核心统一 Query → Item 模型、渠道注册（`eagle.toml`）、多渠道并发路由、跨渠道去重与排序；单个渠道失败不影响其他渠道，错误以 `auth_required` / `rate_limited` / `cooldown` / `busy` / `bad_query` / `unavailable` / `internal` 返回。
- **渠道协议 v1**：任意语言的外部渠道通过 JSON-over-stdio（`describe` / `search`）接入，见 `docs/channel-protocol.md`。
- **X 渠道**：twscrape（自己的账号 cookie）或 twitterapi.io 后端；支持 X 高级搜索语法、长文抓取、预设主题（`eagle x run`）；浏览器扩展 `extensions/x-cookie` 自动同步 cookie。
- **小红书渠道**（Go 外部渠道 `eagle-xhs`）：默认无头 Chrome，快速模式（直达搜索结果页、详情页并行读取）；风控验证后熔断冷却；登录有界面且与搜索互斥。默认不参与搜索，需显式 `-c xhs`。
- **CLI**：`eagle channels`、`eagle search`，以及各渠道原生子命令 `eagle x ...`、`eagle xhs ...`。
- **安装与初始化**：`uv tool install git+https://github.com/81Vm3/eagle_eye` 安装；`eagle setup` 下载并校验对应平台的 `eagle-xhs`、生成配置，`--check` 体检；配置目录按平台（`EAGLE_HOME`）。
- **Agent 接入**：通用 Agent Skill `skills/eagle-search`（遵循 Agent Skills 规范），`eagle skill install`（默认 `~/.agents/skills`，`--agent claude` / `--target`）、`eagle skill show`。
- **发布**：GitHub Release 提供 `eagle-xhs` 的 darwin-arm64、darwin-amd64、linux-amd64、linux-arm64、windows-amd64 二进制与 `checksums.txt`；CI 在 ubuntu 与 macOS 上运行测试。

### 已知限制

- 小红书快速模式不模拟真人节奏，风控/封号风险较高，建议使用小号；搜索耗时为估计值，尚未实测。
- macOS 已通过 CI 单测与安装冒烟测试，浏览器、扩展与扫码登录尚未在真机验证；Windows 原生运行未适配。

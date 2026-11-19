# TEAM.md — eagle_eye Agent 团队

Commander：t69（claude-code，Opus 5.5），只做拆解、委派、只读核验与本文件维护。
工程约定以根目录 `AGENTS.md` 为准；渠道契约见 `docs/channel-protocol.md`。

## Agent 注册表

| 角色 | 启动方式 | 职责 | 工作目录 | 验收标准 |
|---|---|---|---|---|
| ee-core | `hc run --bg --name ee-core --cwd /home/p3g4s/eagle_eye /home/p3g4s/.local/bin/claude --dangerously-skip-permissions "<任务>"` | Python 核心中间层（模型/Channel 接口/注册表/路由/输出/CLI）、X 渠道迁移、ExternalChannel、eagle.toml、skills/（Skill）、AGENTS.md/README、顶层 Makefile | `/home/p3g4s/eagle_eye`（除 `backends/xhs/`） | `python3 -m unittest` 全过；`bin/eagle channels` 列出 x、xhs；`bin/eagle search -c x '...' --limit 3` 实测通过；`eagle x run/topics/article/login` 可用 |
| ee-xhs | `hc run --bg --name ee-xhs --cwd /home/p3g4s/eagle_eye/backends/xhs /home/p3g4s/.local/bin/claude --dangerously-skip-permissions "<任务>"` | 小红书 Go 后端迁移，新增 `channel describe/search` 协议实现（遵守拟人节奏/冷却熔断/浏览器互斥） | `/home/p3g4s/eagle_eye/backends/xhs` | `go vet ./... && go test ./...` 通过；`channel describe` 输出合法 Descriptor；`channel search` 在无浏览器/冷却/忙时返回正确错误码；login 不回归；不做真实小红书搜索 |

## 当前 Agent 通讯录

| id | hc name | pwd | 程序 | 职责/当前任务 | 状态 | tab | 核验时间 |
|---|---|---|---|---|---|---|---|

（2026-11-19：ee-core/t6d、ee-xhs/t6f 首轮任务验收完成，终端已清理；无活动成员。）
（2026-11-19：ee-trial/t6h codex 试用完成，报告 docs/reports/codex-trial.md，终端已清理。）
（2026-11-19：清理任务 t6k/t6m 验收完成，终端已清理；无活动成员。）
（2026-11-19：t6r 放大限额任务验收完成，终端已清理。）
（2026-11-19：t6s 删除限额任务验收完成，终端已清理；无活动成员。）
（2026-11-19：t6t Skill 迁移到 skills/ 验收完成，终端已清理；无活动成员。）
（2026-11-19：t6x 扩展迁移与 X 限额措辞任务验收完成，终端已清理；无活动成员。）
（2026-11-19：t6z/t70 macOS 开箱即用任务验收完成，终端已清理；无活动成员。）
（2026-11-19：t71 Skill 通用化任务验收完成，终端已清理。）
（2026-11-19：t72 headless 任务验收完成，终端已清理。）
（2026-11-19：t76/t77 headless 文档同步与快速模式任务验收完成，终端已清理；无活动成员。）
（2026-11-19：t7f CI 测试隔离修复验收完成，终端已清理；无活动成员。）
（2026-11-19：t7g CI 测试隔离 Chrome 修复验收完成，终端已清理；无活动成员。）

# eagle_eye 渠道协议 v1

eagle_eye 是 Agent 的信息/搜索中间层：核心（Python）只认识统一的 **Query → Item** 模型，
小红书、X 等只是"渠道"。渠道有两种接入方式：

1. **进程内渠道**：Python 类，实现 `eagle_eye.core.Channel` 接口（如 X 渠道）。
2. **外部渠道**：任意语言的可执行程序，按本文的 JSON-over-stdio 协议通信（如小红书 Go 后端）。
   核心用通用的 `ExternalChannel` 适配，新增外部渠道只需在 `eagle.toml` 里登记，不写 Python。

两种方式对上层完全等价：同样的 `describe` 元数据、同样的 Query、同样的 Item、同样的错误码。

## 调用方式

所有渠道都在仓库根目录的 `eagle.toml` 中登记（`EAGLE_CONFIG` 可指向别的文件）。外部渠道：

```toml
[channels.xhs]
type = "external"
command = ["bin/eagle-xhs", "channel", "-config", "config.yaml"]  # 程序路径相对仓库根目录；config.yaml 相对子进程 cwd
cwd = "backends/xhs"            # 可选，子进程工作目录（相对仓库根目录）
timeout_seconds = 900           # 可选，单次 search 超时（默认 300）
describe_timeout_seconds = 30   # 可选，describe 超时（默认 30）
native_command = ["bin/eagle-xhs"]  # 可选，`eagle xhs <参数...>` 原样透传给它（cwd 同上）
enabled = true                  # 可选，默认 true；false 时完全不参与搜索
default = false                 # 可选，默认 true；false 时不带 -c 的 eagle search 不搜它，必须显式 -c xhs
title = "小红书"                # 可选，title / description / cost_hint：
description = "..."             #   程序不可用（未构建、describe 失败）时 eagle channels 的兜底展示
cost_hint = "..."
```

进程内 Python 渠道（不走 stdio，实现 `eagle_eye.core.Channel`，构造签名 `(name, config, root)`）：

```toml
[channels.x]
type = "python"
class = "eagle_eye.channels.x.adapter:XChannel"   # 模块:类
enabled = true
default = true
```

登记规则：

- `enabled` / `default` 对两种渠道都适用：不带 `-c` 的 `eagle search` 只搜 `enabled = true` 且 `default = true` 的渠道；`-c` 显式指定时只要求已登记。
- 路径：`cwd`，以及 `command` / `native_command` 中不以 `-` 开头、不含 `://`、含 `/` 的相对参数，一律按 `eagle.toml` 所在目录（仓库根目录）解析成绝对路径，因此子进程 `cwd` 改变后仍指向同一文件。
- 程序不存在或不可执行时，该渠道报 `unavailable`，不影响其他渠道。
- `describe` / `search` 超时：核心终止子进程组，按 `internal` 处理（附 stderr 末尾若干行）。

核心在 `command` 末尾追加一个动词调用：

| 调用 | stdin | stdout |
|---|---|---|
| `<command...> describe` | 无 | 一个 Descriptor JSON 对象 |
| `<command...> search` | 一个 Query JSON 对象 | 一个 SearchResponse JSON 对象 |

- **stdout 只输出一个 JSON 对象**（UTF-8，可带结尾换行）；进度和日志一律写 stderr。
- 成功 exit 0；失败 exit 非 0，且 stdout 仍输出 `{"protocol":1,"error":{...}}`（无法输出时核心按 `internal` 处理，并附 stderr 末尾若干行）。
- 未知字段必须忽略（向前兼容）。

## Descriptor

```json
{
  "protocol": 1,
  "name": "xhs",
  "title": "小红书",
  "description": "搜索小红书笔记（真实浏览器、拟人节奏，慢，有封号风险）",
  "capabilities": ["search"],
  "query_syntax": "普通关键词，不支持高级语法",
  "cost_hint": "单次搜索约 1–3 分钟；风控后熔断冷却",
  "options": {
    "detail": {"type": "int", "default": 3, "description": "打开前 N 篇笔记读取正文"}
  }
}
```

## Query

```json
{
  "protocol": 1,
  "text": "Claude Code 教程",
  "limit": 20,
  "since_hours": null,
  "sort": "relevance",
  "options": {}
}
```

- `limit`：最多返回条数（渠道可少返回）。
- `since_hours`：只要最近 N 小时；`null`/0 表示不限。渠道不支持时忽略，并在 `meta.ignored` 中说明。
- `sort`：`relevance | time | score`，渠道不支持时按自身默认并在 `meta.ignored` 中说明。
- `options`：渠道私有参数（键名见该渠道 Descriptor 的 `options`）；未知键忽略。

## SearchResponse

```json
{
  "protocol": 1,
  "items": [ /* Item */ ],
  "meta": {"query": "...", "ignored": ["since_hours"], "notes": "..."}
}
```

## Item

```json
{
  "channel": "xhs",
  "id": "65f0...",
  "url": "https://www.xiaohongshu.com/explore/65f0...?xsec_token=...",
  "title": "标题（无则空串）",
  "text": "正文（没抓正文时为空串）",
  "author": {"id": "...", "name": "...", "url": "..."},
  "created_at": "2026-11-19T08:00:00Z",
  "metrics": {"likes": 12, "reposts": 0, "comments": 3, "views": null, "collects": 5},
  "score": 17,
  "lang": null,
  "extra": {}
}
```

- 必填：`channel`、`id`、`url`。其余缺失时用空串 / `null` / `{}`。
- `created_at`：RFC3339（UTC），未知为 `null`。
- `metrics`：整数或 `null`（未知）。各渠道把自家口径映射过来（X：retweets → reposts；小红书：collectedCount → collects）。
- `score`：渠道自定义的热度分（数字或 `null`），核心按 `sort=score` 排序时使用。
- `extra`：渠道私有字段（X 的 followers / isReply / article / hits，小红书的 tags / ip_location / note_type 等）。

## 错误

```json
{"protocol": 1, "error": {"code": "cooldown", "message": "触发风控，冷却至 2026-11-19T20:00:00+08:00", "retry_after_seconds": 21600}}
```

| code | 含义 | Agent 应如何处理 |
|---|---|---|
| `auth_required` | 未登录 / 凭证失效 | 告诉用户需要人工登录，不要重试 |
| `rate_limited` | 平台限速或当日配额用完 | 按 `retry_after_seconds` 之后再试，或换渠道 |
| `cooldown` | 渠道主动熔断冷却（风控信号后） | 同上，绝不绕过 |
| `busy` | 渠道资源被占用（如浏览器正被登录或另一个搜索使用） | 稍后再试或换渠道 |
| `bad_query` | 参数非法 | 修正 Query |
| `unavailable` | 依赖缺失（浏览器连不上、二进制不存在等） | 告诉用户 |
| `internal` | 其他错误 | 附 message 汇报 |

多渠道并发搜索时，单个渠道失败不影响其他渠道；核心把每个渠道的错误汇总到结果的 `errors` 中。

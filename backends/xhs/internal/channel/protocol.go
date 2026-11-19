// Package channel 实现 eagle_eye 渠道协议 v1（docs/channel-protocol.md）的小红书外部渠道：
//
//	eagle-xhs channel [-config path] [-v] describe   → stdout 输出一个 Descriptor
//	eagle-xhs channel [-config path] [-v] search     ← stdin 一个 Query，→ stdout 一个 SearchResponse
//
// stdout 只输出一个 JSON 对象，日志全部写 stderr。失败时 exit 非 0，stdout 仍输出 {"protocol":1,"error":{...}}。
package channel

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"unicode/utf8"
)

// Protocol 渠道协议版本。
const Protocol = 1

// Name 渠道名。
const Name = "xhs"

// detail 选项：打开前 N 篇笔记读取正文/发布时间/标签。
const (
	DefaultDetail = 3
	MaxDetail     = 10
)

// limit：默认 20，渠道最多返回 MaxLimit 条（多要的部分在 meta.notes 里说明）。
const (
	DefaultLimit = 20
	MaxLimit     = 50
)

// MaxTextRunes 关键词长度上限（小红书搜索框本身也有长度限制，过长基本是误用）。
const MaxTextRunes = 100

// 排序
const (
	SortRelevance = "relevance"
	SortTime      = "time"
	SortScore     = "score"
)

// ---------- Descriptor ----------

type OptionSpec struct {
	Type        string `json:"type"`
	Default     any    `json:"default"`
	Description string `json:"description"`
}

type Descriptor struct {
	Protocol     int                   `json:"protocol"`
	Name         string                `json:"name"`
	Title        string                `json:"title"`
	Description  string                `json:"description"`
	Capabilities []string              `json:"capabilities"`
	QuerySyntax  string                `json:"query_syntax"`
	CostHint     string                `json:"cost_hint"`
	Options      map[string]OptionSpec `json:"options"`
}

// Describe 渠道元数据。静态内容，不依赖配置文件（配置缺失时 describe 仍可用）。
func Describe() Descriptor {
	return Descriptor{
		Protocol: Protocol,
		Name:     Name,
		Title:    "小红书",
		Description: "搜索小红书笔记（真实浏览器、拟人节奏，慢，有封号风险，按需使用）。" +
			"返回标题、作者、点赞/评论/收藏数和发布时间；前 detail 篇会打开读正文、标签和 IP 属地。" +
			"同一时刻只允许一个进程使用浏览器：另一次搜索或 login 进行中返回 busy，风控冷却期返回 cooldown。",
		Capabilities: []string{"search"},
		QuerySyntax:  "普通关键词（在搜索框里逐字输入），不支持高级语法；sort=time/score 为在综合排序结果内的本地重排",
		CostHint: "单次搜索约 1–3 分钟（detail 每多 1 篇约多 20–30 秒，页面加载按拟人节奏跨进程节流）；" +
			"触发风控验证后熔断冷却（默认 6 小时，连续触发翻倍），期间返回 cooldown；有封号风险，按需使用；" +
			"耗时为保守的经验估计，未经实测验证",
		Options: map[string]OptionSpec{
			"detail": {Type: "int", Default: DefaultDetail,
				Description: fmt.Sprintf("打开排在最前的 N 篇笔记读取正文、发布时间、标签和 IP 属地（0–%d，默认 %d；每篇约 20–30 秒）", MaxDetail, DefaultDetail)},
		},
	}
}

// ---------- Query ----------

// Query 解析校验后的查询。
type Query struct {
	Text       string
	Limit      int
	SinceHours float64 // 0 表示不限
	Sort       string
	Detail     int
	// LimitCapped 请求的 limit 超过 MaxLimit 时原值（>0 表示被截断）
	LimitCapped int
}

type rawQuery struct {
	Protocol   *int                       `json:"protocol"`
	Text       *string                    `json:"text"`
	Limit      json.RawMessage            `json:"limit"`
	SinceHours json.RawMessage            `json:"since_hours"`
	Sort       *string                    `json:"sort"`
	Options    map[string]json.RawMessage `json:"options"`
}

// ParseQuery 从 r 读一个 Query JSON 并校验。任何不合法都返回 bad_query 错误；未知字段和未知 options 键忽略。
func ParseQuery(r io.Reader) (Query, error) {
	data, err := io.ReadAll(io.LimitReader(r, 1<<20))
	if err != nil {
		return Query{}, badQuery("读取 stdin 失败: %v", err)
	}
	data = bytes.TrimSpace(data)
	if len(data) == 0 {
		return Query{}, badQuery("stdin 为空，需要一个 Query JSON 对象")
	}
	var raw rawQuery
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	if err := dec.Decode(&raw); err != nil {
		return Query{}, badQuery("Query 不是合法的 JSON 对象: %v", err)
	}
	if dec.More() {
		return Query{}, badQuery("stdin 只能包含一个 JSON 对象")
	}

	if raw.Protocol != nil && *raw.Protocol != Protocol {
		return Query{}, badQuery("不支持的协议版本 %d（本渠道实现 v%d）", *raw.Protocol, Protocol)
	}

	q := Query{Limit: DefaultLimit, Sort: SortRelevance, Detail: DefaultDetail}
	if raw.Text != nil {
		q.Text = strings.Join(strings.Fields(*raw.Text), " ")
	}
	if q.Text == "" {
		return Query{}, badQuery("text 不能为空")
	}
	if n := utf8.RuneCountInString(q.Text); n > MaxTextRunes {
		return Query{}, badQuery("text 过长（%d 字，上限 %d）", n, MaxTextRunes)
	}

	if num, set, ok := number(raw.Limit); set {
		n, err := intOf(num)
		if !ok || err != nil || n < 1 {
			return Query{}, badQuery("limit 必须是正整数，收到 %s", string(raw.Limit))
		}
		if n > MaxLimit {
			q.LimitCapped, n = n, MaxLimit
		}
		q.Limit = n
	}

	if num, set, ok := number(raw.SinceHours); set {
		f, err := num.Float64()
		if !ok || err != nil || f < 0 {
			return Query{}, badQuery("since_hours 必须是非负数或 null，收到 %s", string(raw.SinceHours))
		}
		q.SinceHours = f
	}

	if raw.Sort != nil && *raw.Sort != "" {
		switch s := strings.ToLower(strings.TrimSpace(*raw.Sort)); s {
		case SortRelevance, SortTime, SortScore:
			q.Sort = s
		default:
			return Query{}, badQuery("sort 只能是 relevance | time | score，收到 %q", *raw.Sort)
		}
	}

	if num, set, ok := number(raw.Options["detail"]); set {
		n, err := intOf(num)
		if !ok || err != nil || n < 0 || n > MaxDetail {
			return Query{}, badQuery("options.detail 必须是 0–%d 的整数，收到 %s", MaxDetail, string(raw.Options["detail"]))
		}
		q.Detail = n
	}
	return q, nil
}

// number 解析一个 JSON 数字字段。set=false 表示缺省或 null；ok=false 表示不是 JSON 数字（字符串 "3" 也不行）。
func number(v json.RawMessage) (n json.Number, set, ok bool) {
	v = bytes.TrimSpace(v)
	if len(v) == 0 || string(v) == "null" {
		return "", false, false
	}
	if v[0] != '-' && (v[0] < '0' || v[0] > '9') {
		return "", true, false
	}
	d := json.NewDecoder(bytes.NewReader(v))
	d.UseNumber()
	if err := d.Decode(&n); err != nil {
		return "", true, false
	}
	return n, true, true
}

// intOf 接受整数或整数值的浮点（如 20.0），拒绝 2.5。
func intOf(n json.Number) (int, error) {
	if i, err := n.Int64(); err == nil {
		return int(i), nil
	}
	f, err := n.Float64()
	if err != nil || f != float64(int64(f)) {
		return 0, errors.New("不是整数")
	}
	return int(f), nil
}

// ---------- SearchResponse / Item ----------

type Author struct {
	ID   string `json:"id"`
	Name string `json:"name"`
	URL  string `json:"url"`
}

type Metrics struct {
	Likes    *int `json:"likes"`
	Reposts  *int `json:"reposts"`
	Comments *int `json:"comments"`
	Views    *int `json:"views"`
	Collects *int `json:"collects"`
}

type Item struct {
	Channel   string         `json:"channel"`
	ID        string         `json:"id"`
	URL       string         `json:"url"`
	Title     string         `json:"title"`
	Text      string         `json:"text"`
	Author    Author         `json:"author"`
	CreatedAt *string        `json:"created_at"`
	Metrics   Metrics        `json:"metrics"`
	Score     *int           `json:"score"`
	Lang      *string        `json:"lang"`
	Extra     map[string]any `json:"extra"`
}

type Meta struct {
	Query   string   `json:"query"`
	Ignored []string `json:"ignored"`
	Notes   string   `json:"notes"`
}

type Response struct {
	Protocol int    `json:"protocol"`
	Items    []Item `json:"items"`
	Meta     Meta   `json:"meta"`
}

// ---------- 错误 ----------

// 错误码（协议定义）。
const (
	CodeAuthRequired = "auth_required"
	CodeRateLimited  = "rate_limited"
	CodeCooldown     = "cooldown"
	CodeBusy         = "busy"
	CodeBadQuery     = "bad_query"
	CodeUnavailable  = "unavailable"
	CodeInternal     = "internal"
)

// Error 协议错误。
type Error struct {
	Code       string `json:"code"`
	Message    string `json:"message"`
	RetryAfter *int   `json:"retry_after_seconds,omitempty"`
}

func (e *Error) Error() string { return e.Code + ": " + e.Message }

func newErr(code, format string, args ...any) *Error {
	return &Error{Code: code, Message: fmt.Sprintf(format, args...)}
}

func badQuery(format string, args ...any) *Error { return newErr(CodeBadQuery, format, args...) }

// withRetry 设置 retry_after_seconds。
func (e *Error) withRetry(sec int) *Error {
	e.RetryAfter = &sec
	return e
}

type errorResponse struct {
	Protocol int    `json:"protocol"`
	Error    *Error `json:"error"`
}

// AsError 把任意错误转成协议错误（非 *Error 的按 internal）。
func AsError(err error) *Error {
	var e *Error
	if errors.As(err, &e) {
		return e
	}
	return newErr(CodeInternal, "%v", err)
}

// WriteJSON 向 w 写一个 JSON 对象（不转义 HTML 字符，结尾换行）。
func WriteJSON(w io.Writer, v any) error {
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	return enc.Encode(v)
}

// WriteError 输出错误响应。
func WriteError(w io.Writer, err error) error {
	return WriteJSON(w, errorResponse{Protocol: Protocol, Error: AsError(err)})
}

// Errorf 构造协议错误（供命令行入口使用）。
func Errorf(code, format string, args ...any) *Error { return newErr(code, format, args...) }

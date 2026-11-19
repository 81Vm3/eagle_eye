package channel

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"eagle-eye/xhs/internal/config"
	"eagle-eye/xhs/internal/guard"
	"eagle-eye/xhs/internal/lock"
	"eagle-eye/xhs/internal/store"
	"eagle-eye/xhs/internal/xhs"
)

// ---------- Query ----------

func TestParseQueryValid(t *testing.T) {
	q, err := ParseQuery(strings.NewReader(`{"protocol":1,"text":"  Claude   Code 教程 ","limit":null,"since_hours":null,"sort":"relevance","options":{},"future_field":42}`))
	if err != nil {
		t.Fatal(err)
	}
	if q.Text != "Claude Code 教程" || q.Limit != DefaultLimit || q.SinceHours != 0 || q.Sort != SortRelevance || q.Detail != DefaultDetail {
		t.Fatalf("%+v", q)
	}

	q, err = ParseQuery(strings.NewReader(`{"text":"x","limit":200,"since_hours":24.5,"sort":"Score","options":{"detail":0,"unknown":"y"}}`))
	if err != nil {
		t.Fatal(err)
	}
	if q.Limit != MaxLimit || q.LimitCapped != 200 || q.SinceHours != 24.5 || q.Sort != SortScore || q.Detail != 0 {
		t.Fatalf("%+v", q)
	}

	q, err = ParseQuery(strings.NewReader(`{"protocol":1,"text":"x","limit":5.0,"sort":"","options":{"detail":null}}`))
	if err != nil || q.Limit != 5 || q.Sort != SortRelevance || q.Detail != DefaultDetail {
		t.Fatalf("%+v %v", q, err)
	}
	q, err = ParseQuery(strings.NewReader(fmt.Sprintf(`{"text":"x","options":{"detail":%d}}`, MaxDetail)))
	if err != nil || q.Detail != MaxDetail {
		t.Fatalf("%+v %v", q, err)
	}
}

func TestParseQueryBad(t *testing.T) {
	cases := map[string]string{
		"空 stdin":     ``,
		"非 JSON":      `hello`,
		"数组":          `[1]`,
		"两个对象":        `{"text":"a"} {"text":"b"}`,
		"协议版本":        `{"protocol":2,"text":"a"}`,
		"空文本":         `{"protocol":1,"text":""}`,
		"空白文本":        `{"protocol":1,"text":"   "}`,
		"缺少文本":        `{"protocol":1}`,
		"文本类型":        `{"text":123}`,
		"文本过长":        `{"text":"` + strings.Repeat("长", MaxTextRunes+1) + `"}`,
		"limit=0":     `{"text":"a","limit":0}`,
		"limit 负数":    `{"text":"a","limit":-3}`,
		"limit 小数":    `{"text":"a","limit":2.5}`,
		"limit 字符串":   `{"text":"a","limit":"many"}`,
		"limit 数字字符串": `{"text":"a","limit":"20"}`,
		"limit 布尔":    `{"text":"a","limit":true}`,
		"since 负数":    `{"text":"a","since_hours":-1}`,
		"since 字符串":   `{"text":"a","since_hours":"yesterday"}`,
		"未知 sort":     `{"text":"a","sort":"hot"}`,
		"detail 超上限":  fmt.Sprintf(`{"text":"a","options":{"detail":%d}}`, MaxDetail+1),
		"detail 负数":   `{"text":"a","options":{"detail":-1}}`,
		"detail 小数":   `{"text":"a","options":{"detail":1.5}}`,
		"detail 字符串":  `{"text":"a","options":{"detail":"3"}}`,
		"options 非对象": `{"text":"a","options":[1]}`,
	}
	for name, in := range cases {
		_, err := ParseQuery(strings.NewReader(in))
		var e *Error
		if !errors.As(err, &e) || e.Code != CodeBadQuery {
			t.Errorf("%s: want bad_query, got %v", name, err)
		}
	}
}

// ---------- 输出格式 ----------

func TestDescriptorJSON(t *testing.T) {
	var buf bytes.Buffer
	if err := WriteJSON(&buf, Describe()); err != nil {
		t.Fatal(err)
	}
	var d map[string]any
	if err := json.Unmarshal(buf.Bytes(), &d); err != nil {
		t.Fatal(err)
	}
	if d["protocol"].(float64) != 1 || d["name"] != "xhs" || d["title"] != "小红书" {
		t.Fatalf("%v", d)
	}
	if caps := d["capabilities"].([]any); len(caps) != 1 || caps[0] != "search" {
		t.Fatalf("capabilities %v", caps)
	}
	det := d["options"].(map[string]any)["detail"].(map[string]any)
	if det["type"] != "int" || det["default"].(float64) != DefaultDetail || det["description"] == "" {
		t.Fatalf("detail %v", det)
	}
	if strings.Count(strings.TrimSpace(buf.String()), "\n") != 0 {
		t.Fatal("应输出单行 JSON")
	}
}

func TestErrorJSON(t *testing.T) {
	var buf bytes.Buffer
	_ = WriteError(&buf, badQuery("text 不能为空"))
	if got := strings.TrimSpace(buf.String()); got != `{"protocol":1,"error":{"code":"bad_query","message":"text 不能为空"}}` {
		t.Fatal(got)
	}
	buf.Reset()
	_ = WriteError(&buf, newErr(CodeCooldown, "x").withRetry(30))
	if !strings.Contains(buf.String(), `"retry_after_seconds":30`) {
		t.Fatal(buf.String())
	}
	buf.Reset()
	_ = WriteError(&buf, errors.New("boom"))
	if !strings.Contains(buf.String(), `"code":"internal"`) {
		t.Fatal(buf.String())
	}
}

// ---------- Item 映射 ----------

var now = time.Date(2026, 11, 19, 12, 0, 0, 0, time.UTC)

func TestCardItem(t *testing.T) {
	card := xhs.Card{ID: "6af336200000000009022f96", XsecToken: "AB+c=", Title: "标题", Type: "video",
		UserID: "u1", Nickname: "作者", Likes: "1.2万"}
	it := CardItem(card, now)
	if it.Channel != "xhs" || it.ID != card.ID || it.Title != "标题" || it.Text != "" {
		t.Fatalf("%+v", it)
	}
	if !strings.Contains(it.URL, "/explore/"+card.ID) || !strings.Contains(it.URL, "xsec_token=AB%2Bc%3D") ||
		!strings.Contains(it.URL, "xsec_source=pc_search") {
		t.Fatal(it.URL)
	}
	if it.Author.ID != "u1" || it.Author.Name != "作者" || !strings.HasPrefix(it.Author.URL, "https://www.xiaohongshu.com/user/profile/u1") {
		t.Fatalf("%+v", it.Author)
	}
	if *it.Metrics.Likes != 12000 || it.Metrics.Collects != nil || it.Metrics.Comments != nil || it.Metrics.Reposts != nil || it.Metrics.Views != nil {
		t.Fatalf("%+v", it.Metrics)
	}
	if it.Score == nil || *it.Score != 12000 {
		t.Fatal("score")
	}
	if it.CreatedAt == nil || *it.CreatedAt != "2026-11-10T15:20:00Z" || it.Extra["created_at_source"] != TimeFromNoteID {
		t.Fatalf("created_at %v %v", it.CreatedAt, it.Extra)
	}
	if it.Extra["note_type"] != "video" || it.Extra["detail"] != false {
		t.Fatalf("%v", it.Extra)
	}

	// 未知计数 / 非 ObjectId 的 ID / 无作者
	it = CardItem(xhs.Card{ID: "short"}, now)
	if it.Metrics.Likes != nil || it.Score != nil || it.CreatedAt != nil || it.Author.URL != "" {
		t.Fatalf("%+v", it)
	}
	b, _ := json.Marshal(it)
	for _, k := range []string{`"created_at":null`, `"score":null`, `"lang":null`, `"likes":null`, `"text":""`} {
		if !bytes.Contains(b, []byte(k)) {
			t.Errorf("JSON 缺少 %s: %s", k, b)
		}
	}
}

func TestApplyNote(t *testing.T) {
	it := CardItem(xhs.Card{ID: "6af336200000000009022f96", XsecToken: "tok", Title: "", UserID: "u1", Likes: "10"}, now)
	n := &xhs.Note{NoteID: it.ID, XsecToken: "tok2", Title: "详情标题", Desc: "正文内容", Type: "normal",
		Time: time.Date(2026, 11, 10, 15, 20, 0, 0, time.UTC).UnixMilli(), IPLocation: "上海",
		User:         xhs.User{UserID: "u1", Nickname: "作者"},
		InteractInfo: xhs.InteractInfo{LikedCount: "12", CollectedCount: "1.5万", CommentCount: "3", ShareCount: "7"},
		AuthorHref:   "https://www.xiaohongshu.com/user/profile/u1?xsec_token=zz"}
	n.TagList = append(n.TagList, struct {
		Name string `json:"name"`
	}{"Claude"})
	ApplyNote(&it, n)
	if it.Title != "详情标题" || it.Text != "正文内容" || it.Author.Name != "作者" || it.Author.URL != n.AuthorHref {
		t.Fatalf("%+v", it)
	}
	if !strings.Contains(it.URL, "xsec_token=tok2") {
		t.Fatal(it.URL)
	}
	if *it.Metrics.Likes != 12 || *it.Metrics.Collects != 15000 || *it.Metrics.Comments != 3 || *it.Score != 15012 {
		t.Fatalf("%+v score=%v", it.Metrics, *it.Score)
	}
	if *it.CreatedAt != "2026-11-10T15:20:00Z" || it.Extra["created_at_source"] != TimeFromDetail {
		t.Fatal(*it.CreatedAt)
	}
	if it.Extra["detail"] != true || it.Extra["ip_location"] != "上海" || it.Extra["note_type"] != "normal" ||
		it.Extra["shares"] != 7 || len(it.Extra["tags"].([]string)) != 1 {
		t.Fatalf("%v", it.Extra)
	}
	ApplyNote(&it, nil) // nil 安全
}

func mkItem(id string, ageHours float64, likes *int) Item {
	it := Item{ID: id, Extra: map[string]any{}}
	if ageHours >= 0 {
		it.CreatedAt = rfc3339(now.Add(-time.Duration(ageHours * float64(time.Hour))))
	}
	it.Metrics.Likes = likes
	it.Score = scoreOf(it.Metrics)
	return it
}

func ip(n int) *int { return &n }

func TestFilterAndSort(t *testing.T) {
	items := []Item{mkItem("old", 100, ip(50)), mkItem("new", 1, ip(5)), mkItem("unknown", -1, nil), mkItem("mid", 20, ip(500))}
	out, unknown := FilterSince(items, 24, now)
	if len(out) != 3 || unknown != 1 || out[0].ID != "new" || out[1].ID != "unknown" || out[2].ID != "mid" {
		t.Fatalf("%v %d", ids(out), unknown)
	}
	if out, _ := FilterSince(items, 0, now); len(out) != 4 {
		t.Fatal("0 不过滤")
	}
	if len(items) != 4 || items[0].ID != "old" {
		t.Fatal("FilterSince 不应修改输入")
	}

	s := append([]Item(nil), items...)
	SortItems(s, SortTime)
	if got := strings.Join(ids(s), ","); got != "new,mid,old,unknown" {
		t.Fatal(got)
	}
	s = append([]Item(nil), items...)
	SortItems(s, SortScore)
	if got := strings.Join(ids(s), ","); got != "mid,old,new,unknown" {
		t.Fatal(got)
	}
	s = append([]Item(nil), items...)
	SortItems(s, SortRelevance)
	if got := strings.Join(ids(s), ","); got != "old,new,unknown,mid" {
		t.Fatal(got)
	}
}

func ids(items []Item) []string {
	var out []string
	for _, it := range items {
		out = append(out, it.ID)
	}
	return out
}

// ---------- 防封检查 / 错误码（不需要浏览器） ----------

type fixture struct {
	r      *Runner
	db     *store.Store
	cfg    *config.Config
	opened int
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	yaml := "db_path: data/xhs.db\nbrowser:\n  cdp_url: http://127.0.0.1:1\n"
	if err := os.WriteFile(path, []byte(yaml), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg, err := config.Load(path)
	if err != nil {
		t.Fatal(err)
	}
	db, err := store.Open(cfg.DBPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	f := &fixture{db: db, cfg: cfg}
	f.r = NewRunner(cfg, db, slog.New(slog.NewTextHandler(io.Discard, nil)))
	f.r.Now = func() time.Time { return now }
	f.r.Open = func(config.BrowserConfig) (*xhs.Session, error) {
		f.opened++
		return nil, errors.New("connection refused")
	}
	return f
}

func (f *fixture) search(t *testing.T) *Error {
	t.Helper()
	resp, err := f.r.Search(context.Background(), Query{Text: "x", Limit: 20, Sort: SortRelevance, Detail: 3})
	if resp != nil {
		t.Fatal("测试里不应走到浏览器之后")
	}
	var e *Error
	if !errors.As(err, &e) {
		t.Fatalf("应返回 *Error: %v", err)
	}
	return e
}

func TestSearchUnavailableReleasesLock(t *testing.T) {
	f := newFixture(t)
	e := f.search(t)
	if e.Code != CodeUnavailable || f.opened != 1 {
		t.Fatalf("%+v opened=%d", e, f.opened)
	}
	// 渠道没有搜索次数/间隔限制：立即重试照常走到浏览器
	if e := f.search(t); e.Code != CodeUnavailable || f.opened != 2 {
		t.Fatalf("立即重试应仍是 unavailable: %+v opened=%d", e, f.opened)
	}
	// 锁已释放
	lk, err := lock.TryLock(f.cfg.LockPath(), "test")
	if err != nil {
		t.Fatalf("搜索结束后应释放锁: %v", err)
	}
	_ = lk.Unlock()
}

func TestSearchRequiresCDP(t *testing.T) {
	f := newFixture(t)
	f.cfg.Browser.CDPURL = ""
	if e := f.search(t); e.Code != CodeUnavailable || f.opened != 0 || !strings.Contains(e.Message, "cdp_url") {
		t.Fatalf("%+v", e)
	}
}

func TestSearchCooldown(t *testing.T) {
	f := newFixture(t)
	until := now.Add(90 * time.Minute)
	_ = f.db.SetKV(context.Background(), guard.KeyCooldownUntil, until.Format(time.RFC3339))
	e := f.search(t)
	if e.Code != CodeCooldown || e.RetryAfter == nil || *e.RetryAfter != 5400 || f.opened != 0 {
		t.Fatalf("%+v", e)
	}
}

func TestSearchBusy(t *testing.T) {
	f := newFixture(t)
	lk, err := lock.TryLock(f.cfg.LockPath(), "login")
	if err != nil {
		t.Fatal(err)
	}
	defer lk.Unlock()
	e := f.search(t)
	if e.Code != CodeBusy || f.opened != 0 || !strings.Contains(e.Message, "cmd=login") {
		t.Fatalf("%+v", e)
	}
}

func TestFailMapping(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	gone := func() bool { return true }
	alive := func() bool { return false }

	e := f.r.fail(ctx, fmt.Errorf("搜索: %w", xhs.ErrVerification), alive)
	if e.Code != CodeCooldown || e.RetryAfter == nil || *e.RetryAfter != 6*3600 {
		t.Fatalf("%+v", e)
	}
	if until, _ := guard.CooldownUntil(ctx, f.db, now); !until.Equal(now.Add(6 * time.Hour)) {
		t.Fatalf("应写入冷却截止时间: %v", until)
	}
	// 冷却后新的搜索直接返回 cooldown
	if e := f.search(t); e.Code != CodeCooldown {
		t.Fatalf("%+v", e)
	}
	// 连续触发翻倍
	if e := f.r.fail(ctx, xhs.ErrVerification, alive); *e.RetryAfter != 12*3600 {
		t.Fatalf("%+v", e)
	}

	for err, code := range map[error]string{
		xhs.ErrLoginRequired:                        CodeAuthRequired,
		fmt.Errorf("x: %w", xhs.ErrLoginRequired):   CodeAuthRequired,
		xhs.ErrBrowserClosed:                        CodeUnavailable,
		context.Canceled:                            CodeInternal,
		errors.New("页面结构变了"):                        CodeInternal,
		badQuery("passthrough"):                     CodeBadQuery,
		newErr(CodeBusy, "x"):                       CodeBusy,
		fmt.Errorf("y: %w", xhs.ErrNoteUnavailable): CodeInternal,
	} {
		if got := f.r.fail(ctx, err, alive); got.Code != code {
			t.Errorf("%v → %s, want %s", err, got.Code, code)
		}
	}
	if got := f.r.fail(ctx, errors.New("websocket closed"), gone); got.Code != CodeUnavailable {
		t.Errorf("浏览器断开应为 unavailable: %+v", got)
	}
	if got := f.r.fail(ctx, errors.New("x"), nil); got.Code != CodeInternal {
		t.Errorf("%+v", got)
	}
	tctx, cancel := context.WithTimeout(ctx, 0)
	defer cancel()
	<-tctx.Done()
	if got := f.r.fail(tctx, context.DeadlineExceeded, alive); got.Code != CodeInternal || !strings.Contains(got.Message, "超时") {
		t.Errorf("%+v", got)
	}
}

func TestBuildNotes(t *testing.T) {
	s := buildNotes(Query{Text: "x", Limit: 50, LimitCapped: 80, Sort: SortTime, SinceHours: 24}, 40, 2, 1, []string{"额外"})
	for _, want := range []string{"limit=80", "前 40 条", "sort=time", "since_hours", "1 条无法确定", "打开前 2 篇", "额外"} {
		if !strings.Contains(s, want) {
			t.Errorf("notes 缺少 %q: %s", want, s)
		}
	}
}

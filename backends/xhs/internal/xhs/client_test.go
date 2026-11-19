package xhs

import (
	"context"
	"errors"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestSearchURL(t *testing.T) {
	cases := map[string]string{
		"露营":             "keyword=%E9%9C%B2%E8%90%A5",
		"Claude Code 教程": "keyword=Claude%20Code%20%E6%95%99%E7%A8%8B", // 空格是 %20 不是 +
		"a&b=c#d?e/f+g":  "keyword=a%26b%3Dc%23d%3Fe%2Ff%2Bg",
		"-_.!~*'()":      "keyword=-_.!~*'()",
	}
	for kw, want := range cases {
		u := SearchURL(kw)
		if !strings.HasPrefix(u, "https://www.xiaohongshu.com/search_result?"+want+"&source=web_explore_feed") {
			t.Errorf("%q → %s", kw, u)
		}
		pu, err := url.Parse(u)
		if err != nil || pu.Query().Get("keyword") != kw || pu.Query().Get("source") != "web_explore_feed" {
			t.Errorf("%q 解码不回原值: %s %v", kw, pu.Query(), err)
		}
	}
}

func TestBlockedURL(t *testing.T) {
	for u, want := range map[string]error{
		"https://www.xiaohongshu.com/website-login/captcha?redirectPath=x": ErrVerification,
		"https://www.xiaohongshu.com/website-login/error":                  ErrLoginRequired,
		"https://www.xiaohongshu.com/search_result?keyword=a":              nil,
		"":   nil,
		"%%": nil,
	} {
		if got := BlockedURL(u); got != want {
			t.Errorf("%q: %v want %v", u, got, want)
		}
	}
}

// fakeClient 不碰浏览器：eval 按调用次数返回预设结果，地址固定为 curURL。
func fakeClient(curURL *string, eval func(n int, js string) string) (*Client, *int32) {
	var calls int32
	c := &Client{interval: time.Millisecond, searchTimeout: 200 * time.Millisecond,
		moreTimeout: 50 * time.Millisecond, noteTimeout: 200 * time.Millisecond}
	c.evalFn = func(_ context.Context, js string) (string, error) {
		n := int(atomic.AddInt32(&calls, 1))
		return eval(n, js), nil
	}
	c.urlFn = func(context.Context) string { return *curURL }
	c.gotoFn = func(_ context.Context, u string) error { *curURL = u; return nil }
	return c, &calls
}

const feeds2 = `[{"id":"a1","xsecToken":"t1","modelType":"note","noteCard":{"displayTitle":"A"}},
	{"id":"x","modelType":"hot_query"},{"id":"b2","xsecToken":"t2","noteCard":{"displayTitle":"B"}}]`

// 状态未就绪 → 旧关键词的残留结果 → 就绪：轮询到就绪即返回，不睡固定时长。
func TestSearchPollsUntilReady(t *testing.T) {
	cur := ""
	c, calls := fakeClient(&cur, func(n int, js string) string {
		switch n {
		case 1:
			return "" // __INITIAL_STATE__ 还没有
		case 2:
			return `{"kw":"上一个词","feeds":` + feeds2 + `}`
		case 3:
			return `{"kw":"露营","feeds":[]}`
		}
		return `{"kw":"露营","feeds":` + feeds2 + `}`
	})
	cards, note, err := c.Search(context.Background(), "露营")
	if err != nil || note != "" {
		t.Fatal(err, note)
	}
	if len(cards) != 2 || cards[0].ID != "a1" || cards[1].XsecToken != "t2" {
		t.Fatalf("%+v", cards)
	}
	if *calls != 4 {
		t.Fatalf("应在第 4 次检查时就绪，实际 %d 次", *calls)
	}
	if cur != SearchURL("露营") {
		t.Fatalf("应直接导航到搜索结果页: %s", cur)
	}
}

func TestSearchVerificationAndLogin(t *testing.T) {
	// 导航后被重定向到验证页：立即返回 ErrVerification
	cur := ""
	c, _ := fakeClient(&cur, func(int, string) string { return "" })
	c.gotoFn = func(context.Context, string) error {
		cur = "https://www.xiaohongshu.com/website-login/captcha?x=1"
		return nil
	}
	if _, _, err := c.Search(context.Background(), "x"); !errors.Is(err, ErrVerification) {
		t.Fatalf("got %v", err)
	}

	// 轮询中途跳到验证页
	cur = ""
	c, _ = fakeClient(&cur, func(n int, js string) string {
		if n == 2 {
			cur = "https://www.xiaohongshu.com/website-login/captcha"
		}
		return ""
	})
	if _, _, err := c.Search(context.Background(), "x"); !errors.Is(err, ErrVerification) {
		t.Fatalf("got %v", err)
	}

	// 一直没有卡片、也判断不出是否加载完成：未登录 → ErrLoginRequired；已登录 → 超时后返回空结果 + 说明，不报错
	for _, logged := range []bool{false, true} {
		cur = ""
		c, _ = fakeClient(&cur, loginEval(logged, func(int) string { return `{"kw":"x","feeds":[],"done":false}` }))
		cards, note, err := c.Search(context.Background(), "x")
		if !logged {
			if !errors.Is(err, ErrLoginRequired) {
				t.Fatalf("未登录: %v", err)
			}
			continue
		}
		if err != nil || cards == nil || len(cards) != 0 || note != NoteNoResultsTimeout {
			t.Fatalf("已登录超时: %v %v %q", cards, err, note)
		}
	}
}

// loginEval 登录判断的 JS 返回 logged，其余（搜索状态）交给 state(第几次搜索状态查询)。
func loginEval(logged bool, state func(n int) string) func(int, string) string {
	n := 0
	return func(_ int, js string) string {
		if strings.Contains(js, "loggedIn") {
			if logged {
				return "1"
			}
			return ""
		}
		n++
		return state(n)
	}
}

// 零结果：搜索词已一致且页面表明加载完成 → 立即返回空结果（不等超时）；
// 搜索词还是旧的 / 状态里没有搜索词时，done 不能作为"无结果"的依据。
func TestSearchNoResults(t *testing.T) {
	cur := ""
	c, _ := fakeClient(&cur, loginEval(true, func(n int) string {
		switch n {
		case 1:
			return `{"kw":"上一个词","feeds":[],"done":true}`
		case 2:
			return `{"kw":"","feeds":[],"done":true}`
		case 3:
			return `{"kw":"露营","feeds":[],"done":false}` // 还在加载
		}
		return `{"kw":"露营","feeds":[],"done":true}`
	}))
	c.searchTimeout = 5 * time.Second
	start := time.Now()
	cards, note, err := c.Search(context.Background(), "露营")
	if err != nil || cards == nil || len(cards) != 0 || note != NoteNoResults {
		t.Fatalf("%v %v %q", cards, err, note)
	}
	if d := time.Since(start); d > time.Second {
		t.Fatalf("应立即返回，实际等了 %v", d)
	}

	// 加载完成但未登录（未登录时页面也可能没有卡片）→ auth_required，而不是"无结果"
	c, _ = fakeClient(&cur, loginEval(false, func(int) string { return `{"kw":"露营","feeds":[],"done":true}` }))
	if _, _, err := c.Search(context.Background(), "露营"); !errors.Is(err, ErrLoginRequired) {
		t.Fatalf("got %v", err)
	}
}

func TestParseSearchState(t *testing.T) {
	st, err := parseSearchState(`{"kw":"a","feeds":[{"id":"n1"}],"done":true}`, "a")
	if err != nil || !st.Match || !st.Done || len(st.Cards) != 1 {
		t.Fatalf("%+v %v", st, err)
	}
	if st, _ := parseSearchState(`{"kw":"b","feeds":[{"id":"n1"}],"done":true}`, "a"); st.Cards != nil || st.Match {
		t.Fatalf("旧关键词: %+v", st)
	}
	if st, _ := parseSearchState(`{"feeds":[{"id":"n1"}]}`, "a"); len(st.Cards) != 1 || st.Match {
		t.Fatalf("无搜索词: %+v", st)
	}
	if _, err := parseSearchState(`{bad`, "a"); err == nil {
		t.Fatal("应报解析错误")
	}
}

func TestMoreCards(t *testing.T) {
	cur := SearchURL("x")
	scrolled := false
	c, _ := fakeClient(&cur, func(n int, js string) string {
		if strings.Contains(js, "scrollTo") {
			scrolled = true
			return ""
		}
		if n < 4 {
			return `{"kw":"x","feeds":[{"id":"a1"}]}`
		}
		return `{"kw":"x","feeds":[{"id":"a1"},{"id":"b2"}]}`
	})
	cards, grew, err := c.MoreCards(context.Background(), "x", 1)
	if err != nil || !grew || len(cards) != 2 || !scrolled {
		t.Fatalf("%v %v %+v %v", err, grew, cards, scrolled)
	}
	// 没有新卡片：超时后返回 grew=false，不报错
	c, _ = fakeClient(&cur, func(int, string) string { return `{"kw":"x","feeds":[{"id":"a1"}]}` })
	if cards, grew, err := c.MoreCards(context.Background(), "x", 1); err != nil || grew || len(cards) != 1 {
		t.Fatalf("%v %v %+v", err, grew, cards)
	}
}

func TestReadNote(t *testing.T) {
	cur := ""
	c, _ := fakeClient(&cur, func(n int, js string) string {
		if n < 3 {
			return ""
		}
		return `{"note":{"noteId":"a1","title":"T","desc":"D","time":1700000000000,"user":{"userId":"u1","nickname":"N"}},"authorHref":"https://www.xiaohongshu.com/user/profile/u1?xsec_token=z"}`
	})
	note, err := c.ReadNote(context.Background(), Card{ID: "a1", XsecToken: "tok"}, SourceSearch)
	if err != nil {
		t.Fatal(err)
	}
	if note.NoteID != "a1" || note.Desc != "D" || note.XsecToken != "tok" || !strings.Contains(note.AuthorHref, "u1") {
		t.Fatalf("%+v", note)
	}
	if cur != NoteURL("a1", "tok", SourceSearch) || !strings.Contains(cur, "xsec_token=tok") {
		t.Fatalf("应直接打开带 xsec_token 的笔记 URL: %s", cur)
	}

	c, _ = fakeClient(&cur, func(int, string) string { return "!unavailable" })
	if _, err := c.ReadNote(context.Background(), Card{ID: "a1"}, SourceSearch); !errors.Is(err, ErrNoteUnavailable) {
		t.Fatalf("got %v", err)
	}
	c, _ = fakeClient(&cur, func(int, string) string { return "" })
	if _, err := c.ReadNote(context.Background(), Card{ID: "a1"}, SourceSearch); err == nil || !strings.Contains(err.Error(), "超时") {
		t.Fatalf("got %v", err)
	}
}

func TestPoll(t *testing.T) {
	n := 0
	if err := Poll(context.Background(), time.Second, time.Millisecond, func() (bool, error) { n++; return n == 3, nil }); err != nil || n != 3 {
		t.Fatal(err, n)
	}
	if err := Poll(context.Background(), 5*time.Millisecond, time.Millisecond, func() (bool, error) { return false, nil }); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	boom := errors.New("boom")
	if err := Poll(context.Background(), time.Second, time.Millisecond, func() (bool, error) { return false, boom }); err != boom {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := Poll(ctx, time.Second, time.Millisecond, func() (bool, error) { return false, nil }); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}

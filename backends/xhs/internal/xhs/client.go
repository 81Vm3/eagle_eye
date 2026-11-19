package xhs

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strings"
	"time"

	"github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/proto"
)

const (
	siteURL = "https://www.xiaohongshu.com"
	homeURL = siteURL + "/explore"
)

// 等待页面数据就绪的参数：事件驱动（轮询页面状态，就绪即返回），没有固定 sleep。
const (
	pollInterval = 200 * time.Millisecond
	// searchReadyTimeout 打开搜索结果页后等待卡片出现的上限（页面无法判定"无结果"时等满它，按无结果处理）
	searchReadyTimeout = 20 * time.Second
	// moreCardsTimeout 滚动后等待新卡片出现的上限（超时视为没有更多结果）
	moreCardsTimeout = 6 * time.Second
	// noteReadyTimeout 详情页等待笔记数据的上限
	noteReadyTimeout = 15 * time.Second
)

// jsUnwrap 小红书前端是 Vue，状态里的 ref 有 value / _value / _rawValue 几种形态。
const jsUnwrap = `const U = (o) => { if (o === null || o === undefined) return o;
  if (o._rawValue !== undefined) return o._rawValue;
  if (o.value !== undefined) return o.value;
  if (o._value !== undefined) return o._value;
  return o; };`

// loginSelector 侧边栏"我"的入口：出现即已登录。
const loginSelector = `.main-container .user .link-wrapper .channel`

// Client 小红书页面操作（只读，不做点赞等任何互动）。所有数据都从页面已渲染的
// window.__INITIAL_STATE__ 中读取，不自己调用任何签名接口。不模拟真人节奏：直接导航到目标 URL，
// 轮询页面状态，就绪即返回。
type Client struct {
	page *rod.Page

	// 以下可注入（测试用假实现，不碰浏览器）
	evalFn   func(ctx context.Context, js string) (string, error) // 在页面里执行 JS，返回字符串结果
	urlFn    func(ctx context.Context) string                     // 当前主框架地址
	gotoFn   func(ctx context.Context, u string) error            // 导航
	interval time.Duration                                        // 轮询间隔

	searchTimeout, moreTimeout, noteTimeout time.Duration
}

func NewClient(page *rod.Page) *Client {
	c := &Client{page: page, interval: pollInterval,
		searchTimeout: searchReadyTimeout, moreTimeout: moreCardsTimeout, noteTimeout: noteReadyTimeout}
	c.evalFn = c.pageEval
	c.urlFn = c.pageURL
	c.gotoFn = c.pageGoto
	return c
}

func (c *Client) p(ctx context.Context, d time.Duration) *rod.Page {
	return c.page.Context(ctx).Timeout(d)
}

func (c *Client) pageEval(ctx context.Context, js string) (string, error) {
	res, err := c.p(ctx, 10*time.Second).Eval(js)
	if err != nil {
		return "", err
	}
	return res.Value.Str(), nil
}

// pageURL 通过 CDP 读主框架地址（不注入 JS）。
func (c *Client) pageURL(ctx context.Context) string {
	tree, err := (proto.PageGetFrameTree{}).Call(c.p(ctx, 3*time.Second))
	if err != nil || tree == nil || tree.FrameTree == nil || tree.FrameTree.Frame == nil {
		return ""
	}
	return tree.FrameTree.Frame.URL
}

// pageGoto 发起导航，不等 load 事件（图片等资源加载很慢）：之后轮询 __INITIAL_STATE__ 判断数据是否就绪。
func (c *Client) pageGoto(ctx context.Context, u string) error {
	return c.p(ctx, 45*time.Second).Navigate(u)
}

func (c *Client) eval(ctx context.Context, js string) (string, error) { return c.evalFn(ctx, js) }

// Visible 激活本标签页并确认页面可见（只用于接管模式）。窗口被最小化时不去激活（不把窗口弹出来），直接返回 false。
func (c *Client) Visible(ctx context.Context) bool {
	if minimized(ctx, c.page) {
		return false
	}
	if err := (proto.TargetActivateTarget{TargetID: c.page.TargetID}).Call(c.page.Browser().Context(ctx).Timeout(inputTimeout)); err != nil {
		return false
	}
	for i := 0; i < 6; i++ {
		if res, err := c.p(ctx, inputTimeout).Eval(`() => document.visibilityState`); err == nil && res.Value.Str() == "visible" {
			return true
		}
		if sleepCtx(ctx, 500*time.Millisecond) != nil {
			return false
		}
	}
	return false
}

// BlockedURL 地址被重定向到验证页/登录页时返回对应错误。
func BlockedURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return nil
	}
	p := u.EscapedPath()
	switch {
	case strings.HasPrefix(p, "/website-login/captcha"):
		return ErrVerification
	case strings.HasPrefix(p, "/website-login"):
		return ErrLoginRequired
	}
	return nil
}

// CheckBlocked 当前页面若被重定向到验证页/登录页，返回对应错误。
func (c *Client) CheckBlocked(ctx context.Context) error {
	return BlockedURL(c.urlFn(ctx))
}

// Poll 每 interval 调一次 check，直到 check 返回 true（nil）、返回错误、超时（context.DeadlineExceeded）或 ctx 结束。
// 第一次立即检查。
func Poll(ctx context.Context, timeout, interval time.Duration, check func() (bool, error)) error {
	deadline := time.Now().Add(timeout)
	for {
		done, err := check()
		if err != nil {
			return err
		}
		if done {
			return nil
		}
		if !time.Now().Before(deadline) {
			return context.DeadlineExceeded
		}
		if err := sleepCtx(ctx, interval); err != nil {
			return err
		}
	}
}

// poll 页面版 Poll：每次检查前先看是否被重定向到验证页/登录页（风控信号立即返回）。
func (c *Client) poll(ctx context.Context, timeout time.Duration, fn func() bool) error {
	return Poll(ctx, timeout, c.interval, func() (bool, error) {
		if err := c.CheckBlocked(ctx); err != nil {
			return false, err
		}
		return fn(), nil
	})
}

func (c *Client) navigate(ctx context.Context, u string) error {
	if err := c.gotoFn(ctx, u); err != nil {
		if err := c.CheckBlocked(ctx); err != nil {
			return err
		}
		return err
	}
	return c.CheckBlocked(ctx)
}

// loggedInNow 已登录：侧边栏有"我"的入口，或状态里 user.loggedIn 为 true。
func (c *Client) loggedInNow(ctx context.Context) bool {
	s, err := c.eval(ctx, `() => { `+jsUnwrap+`
		if (document.querySelector(`+jsString(loginSelector)+`)) return "1";
		const u = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.user;
		return u && U(u.loggedIn) === true ? "1" : ""; }`)
	return err == nil && s == "1"
}

func jsString(s string) string {
	b, _ := json.Marshal(s)
	return string(b)
}

// WaitForLogin 打开首页，等待用户在浏览器里手动扫码登录（login 子命令用；等人操作，3 秒查一次）。
func (c *Client) WaitForLogin(ctx context.Context, timeout time.Duration) error {
	if err := c.navigate(ctx, homeURL); err != nil && !errors.Is(err, ErrLoginRequired) {
		return err
	}
	err := Poll(ctx, timeout, 3*time.Second, func() (bool, error) { return c.loggedInNow(ctx), nil })
	if errors.Is(err, context.DeadlineExceeded) {
		return fmt.Errorf("等待登录超时")
	}
	return err
}

// ---------- 搜索 ----------

// SearchURL 搜索结果页地址。keyword 按 encodeURIComponent 的规则编码（空格是 %20 而不是 +），
// 与在网页搜索框里回车后浏览器地址栏里的形式一致。
func SearchURL(keyword string) string {
	return siteURL + "/search_result?keyword=" + encodeURIComponent(keyword) + "&source=web_explore_feed"
}

// encodeURIComponent 与 JS 的 encodeURIComponent 相同：只保留 A-Z a-z 0-9 - _ . ! ~ * ' ( )。
func encodeURIComponent(s string) string {
	const keep = "-_.!~*'()"
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		ch := s[i]
		if ch >= 'a' && ch <= 'z' || ch >= 'A' && ch <= 'Z' || ch >= '0' && ch <= '9' || strings.IndexByte(keep, ch) >= 0 {
			b.WriteByte(ch)
			continue
		}
		fmt.Fprintf(&b, "%%%02X", ch)
	}
	return b.String()
}

// jsSearchState 读搜索状态：{"kw": 当前搜索词, "feeds": 卡片数组, "done": 页面是否已表明加载完成}。
//
// done 的判断（**未对线上页面实测**，按小红书 Web 端常见的状态/DOM 结构推测，任一成立即视为加载完成）：
//   - __INITIAL_STATE__.search.hasMore === false 且没有处于加载中（isFetching / loading / fetching 都不为 true）；
//   - 页面出现"无结果"标识（.search-empty / .no-result / .empty-container / .feeds-container .empty 之一）。
//
// 判断不出来时 done=false，调用方等到超时后按无结果处理。
const jsSearchState = `() => { ` + jsUnwrap + `
	const s = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.search;
	if (!s) return "";
	const feeds = s.feeds ? U(s.feeds) : null;
	const fetching = U(s.isFetching) === true || U(s.loading) === true || U(s.fetching) === true;
	const noMore = U(s.hasMore) === false && !fetching;
	const emptyMark = !!document.querySelector('.search-empty, .no-result, .empty-container, .feeds-container .empty');
	return JSON.stringify({kw: U(s.searchValue) || U(s.keyword) || "", feeds: feeds || [], done: noMore || emptyMark}); }`

// searchState jsSearchState 解析后的结果。
type searchState struct {
	Cards []Card
	// Match 状态里的搜索词与关键词一致（不是上一次搜索的残留）
	Match bool
	// Done 页面已表明加载完成（见 jsSearchState）
	Done bool
}

// parseSearchState 解析 jsSearchState 的结果。状态里的搜索词与 keyword 不一致（还是上一次的结果）时不返回卡片；
// 搜索词为空（状态里没有这个字段）时卡片照常返回，但 Match=false（不能据此判定"无结果"）。
func parseSearchState(s, keyword string) (searchState, error) {
	if s == "" {
		return searchState{}, nil
	}
	var st struct {
		KW    string    `json:"kw"`
		Feeds []rawFeed `json:"feeds"`
		Done  bool      `json:"done"`
	}
	if err := json.Unmarshal([]byte(s), &st); err != nil {
		return searchState{}, fmt.Errorf("解析搜索状态: %w", err)
	}
	if st.KW != "" && st.KW != keyword {
		return searchState{}, nil
	}
	return searchState{Cards: toCards(st.Feeds), Match: st.KW == keyword, Done: st.Done}, nil
}

// 无结果时 Search 返回的说明（写进 meta.notes）。
const (
	NoteNoResults        = "小红书没有返回搜索结果（页面已加载完成）"
	NoteNoResultsTimeout = "等待搜索结果超时，未出现任何卡片，按无结果处理（无法判定页面是否加载完成）"
)

// Search 直接打开搜索结果页，轮询 __INITIAL_STATE__ 直到出现与关键词对应的卡片，返回已加载的卡片。
// 没有卡片时返回空切片和说明（NoteNoResults / NoteNoResultsTimeout），不报错：
// 状态里搜索词已与关键词一致且页面表明加载完成 → 立即返回；判断不出 → 等到超时。
// 超时仍没有卡片且未登录 → ErrLoginRequired。
func (c *Client) Search(ctx context.Context, keyword string) ([]Card, string, error) {
	if err := c.navigate(ctx, SearchURL(keyword)); err != nil {
		return nil, "", err
	}
	return c.waitSearch(ctx, keyword)
}

func (c *Client) waitSearch(ctx context.Context, keyword string) ([]Card, string, error) {
	var st searchState
	err := c.poll(ctx, c.searchTimeout, func() bool {
		s, err := c.eval(ctx, jsSearchState)
		if err != nil {
			return false
		}
		st, _ = parseSearchState(s, keyword)
		return len(st.Cards) > 0 || st.Match && st.Done
	})
	switch {
	case err == nil && len(st.Cards) > 0:
		return st.Cards, "", nil
	case err == nil: // 搜索词一致、加载完成、没有卡片
		if !c.loggedInNow(ctx) {
			return nil, "", ErrLoginRequired
		}
		return []Card{}, NoteNoResults, nil
	case errors.Is(err, context.DeadlineExceeded) && ctx.Err() == nil:
		if !c.loggedInNow(ctx) {
			return nil, "", ErrLoginRequired
		}
		return []Card{}, NoteNoResultsTimeout, nil
	}
	return nil, "", err
}

// MoreCards 滚动到页面底部触发加载，等到卡片数超过 have 后返回全部卡片；超时（没有更多结果）返回当前卡片和 false。
func (c *Client) MoreCards(ctx context.Context, keyword string, have int) ([]Card, bool, error) {
	if _, err := c.eval(ctx, `() => { window.scrollTo(0, document.documentElement.scrollHeight); return ""; }`); err != nil {
		return nil, false, err
	}
	var cards []Card
	err := c.poll(ctx, c.moreTimeout, func() bool {
		s, err := c.eval(ctx, jsSearchState)
		if err != nil {
			return false
		}
		if st, _ := parseSearchState(s, keyword); st.Cards != nil {
			cards = st.Cards
		}
		return len(cards) > have
	})
	if errors.Is(err, context.DeadlineExceeded) && ctx.Err() == nil {
		return cards, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	return cards, true, nil
}

// ---------- 笔记详情 ----------

// jsNote 读详情状态：笔记 JSON（附作者链接），笔记不可访问时返回 "!unavailable"，未就绪返回 ""。
func jsNote(id string) string {
	return fmt.Sprintf(`() => { `+jsUnwrap+`
		const n = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.note;
		const m = n && n.noteDetailMap ? U(n.noteDetailMap) : null;
		const d = m && m[%[1]q];
		if (d && d.note && d.note.noteId) {
			const uid = d.note.user && d.note.user.userId;
			const sel = '#noteContainer a[href*="/user/profile/' + uid + '"], .note-container a[href*="/user/profile/' + uid + '"]';
			const a = uid ? document.querySelector(sel) : null;
			return JSON.stringify({note: d.note, authorHref: a ? a.href : ""});
		}
		if (document.querySelector('.access-wrapper, .error-wrapper, .not-found-wrapper, .blocked-wrapper')) return "!unavailable";
		return ""; }`, id)
}

// parseNote 解析 jsNote 的结果；未就绪返回 nil, nil。
func parseNote(s string) (*Note, error) {
	switch s {
	case "":
		return nil, nil
	case "!unavailable":
		return nil, ErrNoteUnavailable
	}
	var v struct {
		Note       Note   `json:"note"`
		AuthorHref string `json:"authorHref"`
	}
	if err := json.Unmarshal([]byte(s), &v); err != nil {
		return nil, fmt.Errorf("解析笔记: %w", err)
	}
	v.Note.AuthorHref = v.AuthorHref
	return &v.Note, nil
}

// ReadNote 在本标签页直接打开笔记详情页（带 xsec_token），轮询直到笔记数据就绪。
func (c *Client) ReadNote(ctx context.Context, card Card, src Source) (*Note, error) {
	if err := c.navigate(ctx, NoteURL(card.ID, card.XsecToken, src)); err != nil {
		return nil, err
	}
	var note *Note
	var nerr error
	err := c.poll(ctx, c.noteTimeout, func() bool {
		s, err := c.eval(ctx, jsNote(card.ID))
		if err != nil {
			return false
		}
		note, nerr = parseNote(s)
		return note != nil || errors.Is(nerr, ErrNoteUnavailable)
	})
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) && ctx.Err() == nil {
			return nil, fmt.Errorf("读取笔记 %s 详情超时", card.ID)
		}
		return nil, err
	}
	if nerr != nil {
		return nil, nerr
	}
	if note.XsecToken == "" {
		note.XsecToken = card.XsecToken
	}
	return note, nil
}

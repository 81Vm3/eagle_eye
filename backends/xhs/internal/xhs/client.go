package xhs

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strings"
	"time"

	"github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/input"
	"github.com/go-rod/rod/lib/proto"
)

const homeURL = "https://www.xiaohongshu.com/explore"

// jsUnwrap 小红书前端是 Vue，状态里的 ref 有 value / _value / _rawValue 几种形态。
const jsUnwrap = `const U = (o) => { if (o === null || o === undefined) return o;
  if (o._rawValue !== undefined) return o._rawValue;
  if (o.value !== undefined) return o.value;
  if (o._value !== undefined) return o._value;
  return o; };`

// Client 小红书页面操作（只读，不做点赞等任何互动）。所有数据都从页面已渲染的
// window.__INITIAL_STATE__ 中读取，不自己调用任何签名接口。
type Client struct {
	page   *rod.Page
	h      *Human
	direct bool // 当前笔记是不是通过 URL 直接打开的（需要后退而不是关弹窗）

	pace *Pacer // 全局导航节流（所有标签页共用），nil 不限速
	fg   *Focus // 前台锁（所有标签页共用），nil 表示单标签页、无需切换
}

func NewClient(page *rod.Page, h *Human) *Client {
	return &Client{page: page, h: h}
}

// Use 设置共享的导航节流器和前台锁。
func (c *Client) Use(p *Pacer, f *Focus) *Client {
	c.pace, c.fg = p, f
	return c
}

// Foreground 让本标签页在 fn 执行期间独占前台（必要时切换过来），fn 里的输入操作不会被其他标签页打断。
func (c *Client) Foreground(ctx context.Context, fn func() error) error {
	release, err := c.fg.Hold(ctx, c.page, c.h)
	if err != nil {
		return err
	}
	defer release()
	return fn()
}

// input 单个输入操作前获取前台；已在 Foreground 作用域内时是空操作。
func (c *Client) input(ctx context.Context) (func(), error) {
	return c.fg.Hold(ctx, c.page, c.h)
}

// Visible 激活本标签页并确认页面可见。窗口被最小化时不去激活（不把窗口弹出来），直接返回 false：
// 不可见的页面不处理输入事件，向它发送合成输入本身就是自动化特征。
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

func (c *Client) p(ctx context.Context, d time.Duration) *rod.Page {
	return c.page.Context(ctx).Timeout(d)
}

func (c *Client) eval(ctx context.Context, js string) (string, error) {
	res, err := c.p(ctx, 10*time.Second).Eval(js)
	if err != nil {
		return "", err
	}
	return res.Value.Str(), nil
}

// currentURL 通过 CDP 读主框架地址（不注入 JS）。
func (c *Client) currentURL(ctx context.Context) string {
	tree, err := (proto.PageGetFrameTree{}).Call(c.p(ctx, 3*time.Second))
	if err != nil || tree == nil || tree.FrameTree == nil || tree.FrameTree.Frame == nil {
		return ""
	}
	return tree.FrameTree.Frame.URL
}

// CheckBlocked 当前页面若被重定向到验证页/登录页，返回对应错误。
func (c *Client) CheckBlocked(ctx context.Context) error {
	u, err := url.Parse(c.currentURL(ctx))
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

// poll 每 500ms 调一次 fn，直到返回 true / 超时 / 被风控拦截。
func (c *Client) poll(ctx context.Context, timeout time.Duration, fn func() bool) error {
	deadline := time.Now().Add(timeout)
	for i := 0; ; i++ {
		if fn() {
			return nil
		}
		if i%4 == 3 {
			if err := c.CheckBlocked(ctx); err != nil {
				return err
			}
		}
		if time.Now().After(deadline) {
			return context.DeadlineExceeded
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(500 * time.Millisecond):
		}
	}
}

func (c *Client) navigate(ctx context.Context, u string) error {
	if err := c.pace.Wait(ctx); err != nil {
		return err
	}
	if err := c.p(ctx, 45*time.Second).Navigate(u); err != nil {
		return err
	}
	_ = c.p(ctx, 30*time.Second).WaitLoad()
	return c.CheckBlocked(ctx)
}

// IsLoggedIn 打开首页并判断是否已登录（侧边栏出现“我”的入口）。
func (c *Client) IsLoggedIn(ctx context.Context) (bool, error) {
	if !strings.HasPrefix(c.currentURL(ctx), "https://www.xiaohongshu.com/") {
		if err := c.navigate(ctx, homeURL); err != nil && err != ErrLoginRequired {
			return false, err
		}
	}
	return c.loggedInNow(ctx), nil
}

func (c *Client) loggedInNow(ctx context.Context) bool {
	has, _, err := c.p(ctx, 5*time.Second).Has(`.main-container .user .link-wrapper .channel`)
	return err == nil && has
}

// WaitForLogin 打开首页，等待用户在浏览器里手动扫码登录。
func (c *Client) WaitForLogin(ctx context.Context, timeout time.Duration) error {
	if err := c.navigate(ctx, homeURL); err != nil && err != ErrLoginRequired {
		return err
	}
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if c.loggedInNow(ctx) {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(3 * time.Second):
		}
	}
	return fmt.Errorf("等待登录超时")
}

// ---------- 列表 ----------

// GoHome 打开发现页并等待推荐流加载。
func (c *Client) GoHome(ctx context.Context) error {
	if err := c.navigate(ctx, homeURL); err != nil {
		return err
	}
	if !c.loggedInNow(ctx) {
		return ErrLoginRequired
	}
	return c.poll(ctx, 20*time.Second, func() bool {
		cards, _ := c.Cards(ctx, SourceFeed)
		return len(cards) > 0
	})
}

// Search 像人一样在搜索框里输入关键词并回车；找不到搜索框时退化为直接打开搜索页。
func (c *Client) Search(ctx context.Context, keyword string) error {
	if !strings.HasPrefix(c.currentURL(ctx), "https://www.xiaohongshu.com/") {
		if err := c.GoHome(ctx); err != nil {
			return err
		}
		if err := c.h.Sleep(ctx, 2*time.Second, 5*time.Second); err != nil {
			return err
		}
	}

	typed := false
	if el, err := c.p(ctx, 5*time.Second).Element(`#search-input`); err == nil {
		err := c.Foreground(ctx, func() error {
			if err := c.h.Click(ctx, c.page, el); err != nil {
				return nil
			}
			_ = el.SelectAllText()
			if err := c.h.Type(ctx, c.page, keyword); err != nil {
				return err
			}
			_ = c.h.Sleep(ctx, 300*time.Millisecond, 1200*time.Millisecond)
			if err := c.pace.Wait(ctx); err != nil { // 回车会加载搜索结果页
				return err
			}
			if err := c.h.Press(ctx, c.page, input.Enter); err == nil {
				typed = true
			}
			return nil
		})
		if err != nil {
			return err
		}
	}
	if !typed {
		q := url.Values{}
		q.Set("keyword", keyword)
		q.Set("source", "web_explore_feed")
		if err := c.navigate(ctx, "https://www.xiaohongshu.com/search_result?"+q.Encode()); err != nil {
			return err
		}
	}

	return c.poll(ctx, 25*time.Second, func() bool {
		if !strings.Contains(c.currentURL(ctx), "/search_result") {
			return false
		}
		s, _ := c.eval(ctx, `() => { `+jsUnwrap+`
			const s = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.search;
			return s ? (U(s.searchValue) || U(s.keyword) || "") : ""; }`)
		cards, _ := c.Cards(ctx, SourceSearch)
		return len(cards) > 0 && (s == "" || s == keyword)
	})
}

// Cards 读取当前页面（推荐流或搜索结果）已加载的卡片。
func (c *Client) Cards(ctx context.Context, src Source) ([]Card, error) {
	key := "feed"
	if src == SourceSearch {
		key = "search"
	}
	s, err := c.eval(ctx, `() => { `+jsUnwrap+`
		const st = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.`+key+`;
		if (!st || !st.feeds) return "";
		const v = U(st.feeds);
		return v ? JSON.stringify(v) : ""; }`)
	if err != nil || s == "" {
		return nil, err
	}
	var raw []rawFeed
	if err := json.Unmarshal([]byte(s), &raw); err != nil {
		return nil, fmt.Errorf("解析卡片: %w", err)
	}
	return toCards(raw), nil
}

// Scroll 向下滚动加载更多。
func (c *Client) Scroll(ctx context.Context) error {
	release, err := c.input(ctx)
	if err != nil {
		return err
	}
	defer release()
	if err := c.h.Scroll(ctx, c.page); err != nil {
		return err
	}
	return c.CheckBlocked(ctx)
}

// ---------- 笔记详情 ----------

// OpenNote 点击卡片打开笔记弹窗；卡片不在 DOM 中时退化为直接打开详情页。
func (c *Client) OpenNote(ctx context.Context, card Card, src Source) (*Note, error) {
	c.direct = false
	el, err := c.p(ctx, 2*time.Second).Element(fmt.Sprintf(`section.note-item a.cover[href*="%s"]`, card.ID))
	if err != nil {
		el, err = c.p(ctx, 1*time.Second).Element(fmt.Sprintf(`section.note-item a[href*="%s"]`, card.ID))
	}
	if err == nil {
		err = c.Foreground(ctx, func() error {
			if err := c.pace.Wait(ctx); err != nil { // 点开笔记会加载详情
				return err
			}
			return c.h.Click(ctx, c.page, el)
		})
	}
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		c.direct = true
		if err := c.navigate(ctx, NoteURL(card.ID, card.XsecToken, src)); err != nil {
			return nil, err
		}
	}
	return c.waitNote(ctx, card)
}

func (c *Client) waitNote(ctx context.Context, card Card) (*Note, error) {
	var note *Note
	err := c.poll(ctx, 15*time.Second, func() bool {
		note, _ = c.readNote(ctx, card.ID)
		return note != nil && note.NoteID != ""
	})
	if err != nil {
		if err == ErrVerification || err == ErrLoginRequired || ctx.Err() != nil {
			return nil, err
		}
		if has, _, _ := c.p(ctx, 2*time.Second).Has(`.access-wrapper, .error-wrapper, .not-found-wrapper, .blocked-wrapper`); has {
			return nil, ErrNoteUnavailable
		}
		return nil, fmt.Errorf("读取笔记 %s 详情超时", card.ID)
	}
	if note.XsecToken == "" {
		note.XsecToken = card.XsecToken
	}
	note.AuthorHref, _ = c.eval(ctx, fmt.Sprintf(`() => {
		const a = document.querySelector('#noteContainer a[href*="/user/profile/%s"], .note-container a[href*="/user/profile/%s"]');
		return a ? a.href : ""; }`, note.User.UserID, note.User.UserID))
	return note, nil
}

func (c *Client) readNote(ctx context.Context, id string) (*Note, error) {
	s, err := c.eval(ctx, fmt.Sprintf(`() => { `+jsUnwrap+`
		const n = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.note;
		if (!n || !n.noteDetailMap) return "";
		const m = U(n.noteDetailMap);
		const d = m && m[%q];
		return d && d.note ? JSON.stringify(d.note) : ""; }`, id))
	if err != nil || s == "" {
		return nil, err
	}
	var note Note
	if err := json.Unmarshal([]byte(s), &note); err != nil {
		return nil, err
	}
	return &note, nil
}

// Dwell 模拟阅读：停留一段时间，期间偶尔在弹窗内滚动（看评论）。
func (c *Client) Dwell(ctx context.Context, min, max time.Duration) error {
	release, err := c.input(ctx)
	if err != nil {
		return err
	}
	defer release()
	total := c.h.jitter(min, max)
	end := time.Now().Add(total)
	for time.Now().Before(end) {
		if c.h.Chance(0.35) {
			if el, err := c.p(ctx, time.Second).Element(`.note-scroller`); err == nil {
				if shape, err := el.Shape(); err == nil {
					b := shape.Box()
					if err := c.h.MoveTo(ctx, c.page, b.X+b.Width/2, b.Y+b.Height/2); err != nil {
						return err
					}
					if err := c.h.Wheel(ctx, c.page, float64(c.h.IntIn(150, 500)), c.h.IntIn(4, 8)); err != nil {
						return err
					}
				}
			}
		}
		if err := c.h.Sleep(ctx, 1500*time.Millisecond, 4*time.Second); err != nil {
			return err
		}
	}
	return c.CheckBlocked(ctx)
}

// CloseNote 关闭笔记弹窗（Esc），直接打开的则后退。只有按键/点击需要前台，后退不占前台。
func (c *Client) CloseNote(ctx context.Context) error {
	if !c.direct {
		closed := false
		err := c.Foreground(ctx, func() error {
			_ = c.h.Press(ctx, c.page, input.Escape)
			_ = c.h.Sleep(ctx, 500*time.Millisecond, 1200*time.Millisecond)
			if !c.noteModalOpen(ctx) {
				closed = true
				return nil
			}
			if el, err := c.p(ctx, time.Second).Element(`.close-circle, .close-mask-dark`); err == nil {
				_ = c.h.Click(ctx, c.page, el)
				_ = c.h.Sleep(ctx, 400*time.Millisecond, 900*time.Millisecond)
				closed = !c.noteModalOpen(ctx)
			}
			return nil
		})
		if err != nil {
			return err
		}
		if closed {
			return c.CheckBlocked(ctx)
		}
	}
	c.direct = false
	if err := c.pace.Wait(ctx); err != nil {
		return err
	}
	if err := c.p(ctx, 20*time.Second).NavigateBack(); err != nil {
		return err
	}
	_ = c.h.Sleep(ctx, time.Second, 2*time.Second)
	return c.CheckBlocked(ctx)
}

func (c *Client) noteModalOpen(ctx context.Context) bool {
	has, _, err := c.p(ctx, 2*time.Second).Has(`#noteContainer, .note-detail-mask`)
	return err == nil && has
}

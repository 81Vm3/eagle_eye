package channel

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"eagle-eye/xhs/internal/config"
	"eagle-eye/xhs/internal/guard"
	"eagle-eye/xhs/internal/lock"
	"eagle-eye/xhs/internal/store"
	"eagle-eye/xhs/internal/xhs"
)

// Runner 执行一次渠道搜索。每次 channel search 都是一个新进程，所以所有防封状态
// （冷却、页面加载节流时刻、浏览器锁）都从 SQLite / 锁文件里取，跨进程共享。
type Runner struct {
	Cfg *config.Config
	DB  *store.Store
	Log *slog.Logger

	// Now 当前时间（测试可替换）
	Now func() time.Time
	// Open 连接浏览器（测试可替换，避免真的连 Chrome）
	Open func(config.BrowserConfig) (*xhs.Session, error)
}

func NewRunner(cfg *config.Config, db *store.Store, log *slog.Logger) *Runner {
	return &Runner{Cfg: cfg, DB: db, Log: log, Now: time.Now, Open: xhs.OpenBrowser}
}

func (r *Runner) now() time.Time { return r.Now() }

// bg 写入防封状态用的 ctx：即使搜索被中断/超时，冷却和导航时刻也必须落库。
func bg(ctx context.Context) context.Context { return context.WithoutCancel(ctx) }

// checkCooldown 冷却期内直接拒绝，不碰浏览器。
func (r *Runner) checkCooldown(ctx context.Context) error {
	now := r.now()
	until, err := guard.CooldownUntil(ctx, r.DB, now)
	if err != nil {
		return newErr(CodeInternal, "读取冷却状态: %v", err)
	}
	if !until.IsZero() {
		return newErr(CodeCooldown, "小红书触发过风控验证，冷却中，至 %s 之前不访问页面", until.Format(time.RFC3339)).
			withRetry(guard.Seconds(until.Sub(now)))
	}
	return nil
}

// fail 把浏览器流程中的错误映射成协议错误；触发验证页时进入冷却（连续触发翻倍，持久化）。
// browserGone 报告浏览器是否已断开（nil 表示还没连上浏览器）。
func (r *Runner) fail(ctx context.Context, err error, browserGone func() bool) *Error {
	var pe *Error
	switch {
	case errors.As(err, &pe):
		return pe
	case errors.Is(err, xhs.ErrVerification):
		now := r.now()
		until, kvErr := guard.EnterCooldown(bg(ctx), r.DB, r.Cfg.Behavior.CooldownHours, now)
		if kvErr != nil {
			r.Log.Error("写入冷却状态失败", "err", kvErr)
		}
		r.Log.Error("⚠️ 触发小红书安全验证，进入冷却。请在浏览器里手动完成验证。", "冷却到", until.Format("01-02 15:04"))
		return newErr(CodeCooldown, "触发小红书安全验证（风控），已熔断冷却至 %s；需要人工在浏览器里完成验证，冷却期内不要重试",
			until.Format(time.RFC3339)).withRetry(guard.Seconds(until.Sub(now)))
	case errors.Is(err, xhs.ErrLoginRequired):
		return newErr(CodeAuthRequired, "小红书未登录或登录态失效：需要人工执行 eagle-xhs login 扫码登录（程序绝不自动登录）")
	case errors.Is(err, xhs.ErrBrowserClosed):
		return newErr(CodeUnavailable, "%v", err)
	case errors.Is(err, context.Canceled):
		return newErr(CodeInternal, "搜索被中断")
	case ctx.Err() != nil && errors.Is(err, context.DeadlineExceeded):
		return newErr(CodeInternal, "搜索超时（channel.timeout_seconds=%d）", r.Cfg.Channel.TimeoutSeconds)
	case browserGone != nil && browserGone():
		return newErr(CodeUnavailable, "Chrome 已关闭或调试连接断开: %v", err)
	}
	return newErr(CodeInternal, "%v", err)
}

// fatal 打开笔记时必须立刻终止整个搜索的错误。
func fatal(ctx context.Context, err error) bool {
	return ctx.Err() != nil || errors.Is(err, xhs.ErrVerification) || errors.Is(err, xhs.ErrLoginRequired) ||
		errors.Is(err, xhs.ErrBrowserClosed)
}

// Search 执行一次搜索。返回的 error 总是 *Error。
func (r *Runner) Search(ctx context.Context, q Query) (*Response, error) {
	cfg := r.Cfg
	if cfg.Browser.CDPURL == "" {
		return nil, newErr(CodeUnavailable, "channel search 只接管已在运行的 Chrome（新开一个标签页工作），"+
			"请在配置里设置 browser.cdp_url，见 README「接管已有的 Chrome」")
	}

	// 1. 冷却预检（只读，不占锁）：冷却比 busy 更有信息量
	if err := r.checkCooldown(ctx); err != nil {
		return nil, err
	}

	// 2. 浏览器互斥：拿不到立即返回 busy
	lk, err := lock.TryLock(cfg.LockPath(), "channel search")
	if err != nil {
		if errors.Is(err, lock.ErrLocked) {
			return nil, newErr(CodeBusy, "%v：小红书浏览器同一时刻只允许一个进程使用（login 或另一个 channel search 运行期间不可用）", err)
		}
		return nil, newErr(CodeInternal, "%v", err)
	}
	defer lk.Unlock()

	// 3. 锁内复查冷却（等锁期间可能有别的进程触发了风控）
	if err := r.checkCooldown(ctx); err != nil {
		return nil, err
	}

	tctx, cancel := context.WithTimeout(ctx, time.Duration(cfg.Channel.TimeoutSeconds)*time.Second)
	defer cancel()

	// 4. 浏览器：只在 CDP 接管的 Chrome 里新开一个标签页，结束时只关这个标签页
	sess, err := r.Open(cfg.Browser)
	if err != nil {
		return nil, newErr(CodeUnavailable, "连不上 Chrome（browser.cdp_url=%s）: %v", cfg.Browser.CDPURL, err)
	}
	defer sess.Close()
	gone := func() bool { return !xhs.BrowserAlive(bg(ctx), sess.Browser) }

	// 跨进程节流：接着上一个进程最近一次导航的时刻算 nav_interval_seconds，并把自己的导航时刻写回去
	b := cfg.Behavior
	pace := xhs.NewPacer(time.Duration(b.NavIntervalSeconds.Min())*time.Second,
		time.Duration(b.NavIntervalSeconds.Max())*time.Second, b.NavPerMinute)
	if t, _ := guard.GetTime(tctx, r.DB, guard.KeyLastNav); !t.IsZero() {
		pace.Seed(t)
	}
	pace.OnReserve(func(t time.Time) { _ = guard.SetTime(bg(ctx), r.DB, guard.KeyLastNav, t) })
	h := xhs.NewHuman()
	c := xhs.NewClient(sess.Page, h).Use(pace, xhs.NewFocus(r.Log))

	if cfg.Browser.Silent {
		if err := sess.Hide(tctx); err != nil {
			return nil, newErr(CodeUnavailable, "静默模式: %v", err)
		}
		r.Log.Info("静默模式：Chrome 窗口已移到屏幕外，结束时放回")
	}

	ok, err := c.IsLoggedIn(tctx)
	if err != nil {
		return nil, r.fail(tctx, err, gone)
	}
	if !ok {
		return nil, r.fail(tctx, xhs.ErrLoginRequired, gone)
	}
	if !c.Visible(tctx) {
		return nil, newErr(CodeUnavailable, "Chrome 窗口被最小化或不可见，无法拟人输入；请恢复窗口，或开启 browser.silent")
	}
	if err := h.Sleep(tctx, 2*time.Second, 5*time.Second); err != nil { // 先看一眼首页
		return nil, r.fail(tctx, err, gone)
	}

	// 5. 搜索：搜索框逐字输入 + 回车
	r.Log.Info("开始搜索", "关键词", q.Text, "limit", q.Limit, "detail", q.Detail)
	if err := c.Search(tctx, q.Text); err != nil {
		return nil, r.fail(tctx, fmt.Errorf("搜索 %q: %w", q.Text, err), gone)
	}
	if err := r.pause(tctx, h); err != nil {
		return nil, r.fail(tctx, err, gone)
	}

	// 6. 收集卡片：只读已渲染的 __INITIAL_STATE__，不够时像人一样向下滚动
	now := r.now()
	var items []Item
	cards := map[string]xhs.Card{}
	stagnant := 0
	for i := 0; ; i++ {
		cs, err := c.Cards(tctx, xhs.SourceSearch)
		if err != nil {
			return nil, r.fail(tctx, err, gone)
		}
		added := 0
		for _, card := range cs {
			if _, dup := cards[card.ID]; dup {
				continue
			}
			cards[card.ID] = card
			items = append(items, CardItem(card, now))
			added++
		}
		if i > 0 && added == 0 {
			stagnant++
		} else {
			stagnant = 0
		}
		usable, _ := FilterSince(items, q.SinceHours, now)
		if len(usable) >= q.Limit || i >= cfg.Channel.MaxScrolls || stagnant >= 2 {
			break
		}
		if err := c.Scroll(tctx); err != nil {
			return nil, r.fail(tctx, err, gone)
		}
		if err := h.Sleep(tctx, 1200*time.Millisecond, 3500*time.Millisecond); err != nil {
			return nil, r.fail(tctx, err, gone)
		}
	}
	scanned := len(items)
	items, unknown := FilterSince(items, q.SinceHours, now)
	SortItems(items, q.Sort)
	if len(items) > q.Limit {
		items = items[:q.Limit]
	}
	r.Log.Info("结果", "扫描", scanned, "保留", len(items))

	// 7. detail：在列表里点开前 N 篇阅读（拟人停留），读正文/发布时间/标签；不点赞、不做任何互动
	var notes []string
	opened, fails := 0, 0
	for i := 0; i < min(q.Detail, len(items)); i++ {
		card := cards[items[i].ID]
		note, err := c.OpenNote(tctx, card, xhs.SourceSearch)
		if err != nil {
			if fatal(tctx, err) {
				return nil, r.fail(tctx, err, gone)
			}
			r.Log.Warn("打开笔记失败", "id", card.ID, "err", err)
			items[i].Extra["detail_error"] = err.Error()
			if err := c.CloseNote(tctx); err != nil && fatal(tctx, err) {
				return nil, r.fail(tctx, err, gone)
			}
			if fails++; fails >= 2 {
				notes = append(notes, "连续 2 篇笔记打开失败，停止读取正文")
				break
			}
			continue
		}
		fails = 0
		rs := cfg.Behavior.ReadSeconds
		if err := c.Dwell(tctx, time.Duration(rs.Min())*time.Second, time.Duration(rs.Max())*time.Second); err != nil {
			return nil, r.fail(tctx, err, gone)
		}
		ApplyNote(&items[i], note)
		opened++
		if err := c.CloseNote(tctx); err != nil {
			if fatal(tctx, err) {
				return nil, r.fail(tctx, err, gone)
			}
			r.Log.Warn("关闭笔记失败", "err", err)
		}
		if err := r.pause(tctx, h); err != nil {
			return nil, r.fail(tctx, err, gone)
		}
	}
	if q.SinceHours > 0 && opened > 0 { // 详情里的发布时间是最终依据
		items, _ = FilterSince(items, q.SinceHours, now)
	}

	if items == nil {
		items = []Item{} // 协议要求数组，不能是 null
	}
	// 完整走完一次搜索没有触发验证：清零连续触发计数
	if err := guard.ResetCooldownStreak(bg(ctx), r.DB); err != nil {
		r.Log.Warn("清零冷却连续触发计数失败", "err", err)
	}
	return &Response{Protocol: Protocol, Items: items,
		Meta: Meta{Query: q.Text, Ignored: []string{}, Notes: buildNotes(q, scanned, opened, unknown, notes)}}, nil
}

func (r *Runner) pause(ctx context.Context, h *xhs.Human) error {
	d := r.Cfg.Behavior.ActionDelayMs
	return h.Sleep(ctx, time.Duration(d.Min())*time.Millisecond, time.Duration(d.Max())*time.Millisecond)
}

// buildNotes meta.notes：说明各参数实际是怎么生效的。
func buildNotes(q Query, scanned, opened, unknown int, extra []string) string {
	var ns []string
	if q.LimitCapped > 0 {
		ns = append(ns, fmt.Sprintf("limit=%d 超过渠道上限，按 %d 处理", q.LimitCapped, MaxLimit))
	}
	ns = append(ns, fmt.Sprintf("扫描了综合排序的前 %d 条搜索结果", scanned))
	if q.Sort != SortRelevance {
		ns = append(ns, fmt.Sprintf("sort=%s 是在这些结果内的本地重排（未使用小红书筛选面板）", q.Sort))
	}
	if q.SinceHours > 0 {
		s := "since_hours 按发布时间过滤"
		if unknown > 0 {
			s += fmt.Sprintf("（%d 条无法确定发布时间，未过滤）", unknown)
		}
		ns = append(ns, s)
	}
	ns = append(ns, fmt.Sprintf("打开前 %d 篇读取了正文/标签/IP 属地，其余 text 为空", opened))
	ns = append(ns, "未打开的笔记 created_at 由笔记 ID 推算（extra.created_at_source=note_id，与详情页发布时间一致）")
	ns = append(ns, extra...)
	return strings.Join(ns, "；")
}

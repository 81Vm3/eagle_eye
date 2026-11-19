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

// Runner 执行一次渠道搜索。每次 channel search 都是一个新进程，所以风控状态
// （冷却、浏览器锁）都从 SQLite / 锁文件里取，跨进程共享。
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

// bg 写入风控状态用的 ctx：即使搜索被中断/超时，冷却状态也必须落库。
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

// Search 执行一次搜索。返回的 error 总是 *Error。
func (r *Runner) Search(ctx context.Context, q Query) (*Response, error) {
	cfg := r.Cfg
	if !cfg.Browser.Headless && cfg.Browser.CDPURL == "" {
		return nil, newErr(CodeUnavailable, "browser.headless=false 时 channel search 只接管已在运行的 Chrome（新开一个标签页工作），"+
			"请在配置里设置 browser.cdp_url（见 README「接管已有的 Chrome」），或把 browser.headless 设为 true")
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

	// 4. 浏览器：headless 时用专用 profile 启动无头 Chrome、结束时关闭；
	//    否则只在 CDP 接管的 Chrome 里新开一个标签页，结束时只关这个标签页
	sess, err := r.Open(cfg.Browser)
	if err != nil {
		if cfg.Browser.Headless {
			return nil, newErr(CodeUnavailable, "启动无头 Chrome 失败（browser.headless=true，profile=%s）: %v", cfg.Browser.UserDataDir, err)
		}
		return nil, newErr(CodeUnavailable, "连不上 Chrome（browser.cdp_url=%s）: %v", cfg.Browser.CDPURL, err)
	}
	defer sess.Close()
	gone := func() bool { return !xhs.BrowserAlive(bg(ctx), sess.Browser) }

	// 不模拟真人节奏：直接导航到目标 URL，轮询 __INITIAL_STATE__ 就绪即继续，没有任何故意的停顿
	c := xhs.NewClient(sess.Page)
	hide, checkVisible := windowChecks(sess.Headless, cfg.Browser.Silent)
	if sess.Headless {
		r.Log.Info("无头模式：用专用 profile 启动的 Chrome（--headless=new），结束时关闭", "profile", cfg.Browser.UserDataDir)
		if cfg.Browser.Silent {
			r.Log.Debug("无头模式没有窗口，忽略 browser.silent")
		}
	}

	if hide {
		hidden, err := sess.Hide(tctx)
		if err != nil {
			return nil, newErr(CodeUnavailable, "静默模式: %v", err)
		}
		if hidden {
			r.Log.Info("静默模式：Chrome 窗口已移到屏幕外，结束时放回")
		} else {
			r.Log.Warn("静默模式无效：窗口管理器把 Chrome 窗口限制在屏幕内（macOS 常见），窗口保持可见照常工作；" +
				"建议把 browser.silent 设为 false")
		}
	}
	if checkVisible && !c.Visible(tctx) {
		return nil, newErr(CodeUnavailable, "Chrome 窗口被最小化或不可见；请恢复窗口，或开启 browser.silent")
	}

	// 5. 搜索：直接打开搜索结果页，等卡片出现（未登录 → auth_required，验证页 → cooldown）
	start := time.Now()
	r.Log.Info("开始搜索", "关键词", q.Text, "limit", q.Limit, "detail", q.Detail)
	cs, emptyNote, err := c.Search(tctx, q.Text)
	if err != nil {
		return nil, r.fail(tctx, fmt.Errorf("搜索 %q: %w", q.Text, err), gone)
	}
	var notes []string
	if emptyNote != "" {
		notes = append(notes, emptyNote)
	}

	// 6. 收集卡片：不够时滚动到底部，等新卡片出现（没有随机停顿）
	now := r.now()
	var items []Item
	cards := map[string]xhs.Card{}
	for i := 0; ; i++ {
		for _, card := range cs {
			if _, dup := cards[card.ID]; dup {
				continue
			}
			cards[card.ID] = card
			items = append(items, CardItem(card, now))
		}
		usable, _ := FilterSince(items, q.SinceHours, now)
		if len(usable) >= q.Limit || i >= cfg.Channel.MaxScrolls || len(cs) == 0 {
			break
		}
		more, grew, err := c.MoreCards(tctx, q.Text, len(cs))
		if err != nil {
			return nil, r.fail(tctx, err, gone)
		}
		if !grew {
			break // 没有更多结果
		}
		cs = more
	}
	scanned := len(items)
	items, unknown := FilterSince(items, q.SinceHours, now)
	SortItems(items, q.Sort)
	if len(items) > q.Limit {
		items = items[:q.Limit]
	}
	r.Log.Info("结果", "扫描", scanned, "保留", len(items), "耗时", time.Since(start).Round(time.Millisecond).String())

	// 7. detail：用带 xsec_token 的笔记 URL 在新标签页直接打开前 N 篇（最多并行 detail_concurrency 个），
	//    读完立即关闭标签页；不点赞、不做任何互动。任一篇触发验证页 → 立即停止所有标签页
	opened := 0
	if n := min(q.Detail, len(items)); n > 0 {
		targets := make([]xhs.Card, n)
		for i := range targets {
			targets[i] = cards[items[i].ID]
		}
		res, err := fetchDetails(tctx, targets, cfg.Channel.DetailConcurrency, func(ctx context.Context, card xhs.Card) (*xhs.Note, error) {
			page, err := sess.NewPage(ctx)
			if err != nil {
				return nil, err
			}
			defer xhs.ClosePage(page)
			return xhs.NewClient(page).ReadNote(ctx, card, xhs.SourceSearch)
		})
		if err != nil {
			return nil, r.fail(tctx, err, gone)
		}
		failed := 0
		for i, nr := range res {
			switch {
			case nr.Note != nil:
				ApplyNote(&items[i], nr.Note)
				opened++
			case nr.Err != nil:
				r.Log.Warn("读取笔记失败", "id", items[i].ID, "err", nr.Err)
				items[i].Extra["detail_error"] = nr.Err.Error()
				failed++
			}
		}
		if failed > 0 {
			notes = append(notes, fmt.Sprintf("%d 篇笔记读取正文失败（见 extra.detail_error）", failed))
		}
		r.Log.Info("详情", "读取", opened, "失败", failed, "累计耗时", time.Since(start).Round(time.Millisecond).String())
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

// windowChecks 是否需要静默移窗、是否需要确认窗口可见。无头 Chrome 没有窗口，两者都跳过。
func windowChecks(headless, silent bool) (hide, checkVisible bool) {
	if headless {
		return false, false
	}
	return silent, true
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

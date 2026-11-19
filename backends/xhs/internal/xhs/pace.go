package xhs

import (
	"context"
	"io"
	"log/slog"
	"math/rand/v2"
	"sync"
	"time"

	"github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/proto"
)

// Pacer 全局导航节流：所有标签页的页面加载共用一个节奏（同一个账号，平台看到的是总频率）。
// 规则：相邻两次加载之间至少间隔 [minGap, maxGap] 内的随机时长，且任意 60 秒内不超过 perMinute 次。
// 调用方按到达顺序排队，ctx 取消时放弃等待。
type Pacer struct {
	mu        sync.Mutex
	minGap    time.Duration
	maxGap    time.Duration
	perMinute int
	rng       *rand.Rand
	last      time.Time   // 最近一次放行（或已预约）的时刻
	window    []time.Time // 最近 60 秒内已放行/预约的时刻

	now   func() time.Time
	sleep func(ctx context.Context, d time.Duration) error

	onReserve func(time.Time) // 每次预约后回调（持久化最近一次导航时刻，供其他进程节流）
}

// NewPacer perMinute <= 0 表示不限每分钟次数；minGap/maxGap 都为 0 表示不限间隔。
func NewPacer(minGap, maxGap time.Duration, perMinute int) *Pacer {
	if maxGap < minGap {
		maxGap = minGap
	}
	return &Pacer{
		minGap: minGap, maxGap: maxGap, perMinute: perMinute,
		rng:   rand.New(rand.NewPCG(uint64(time.Now().UnixNano()), rand.Uint64())),
		now:   time.Now,
		sleep: sleepCtx,
	}
}

func sleepCtx(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return ctx.Err()
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// Seed 用其他进程留下的最近一次导航时刻初始化（跨进程节流：每个进程的 Pacer 都在内存里，
// 新进程启动时从持久化的时刻接着算间隔）。比已有记录早的时刻忽略。
func (p *Pacer) Seed(last time.Time) {
	if p == nil || last.IsZero() {
		return
	}
	p.mu.Lock()
	if now := p.now(); last.After(now.Add(p.maxGap + time.Minute)) {
		last = now // 记录在遥远的未来（时钟被调过）：按"刚刚导航过"处理，不会睡很久
	}
	if last.After(p.last) {
		p.last = last
	}
	p.mu.Unlock()
}

// OnReserve 每次预约到导航时刻后调用 fn(时刻)，用于持久化。fn 在锁外、等待前调用。
func (p *Pacer) OnReserve(fn func(time.Time)) {
	if p == nil {
		return
	}
	p.mu.Lock()
	p.onReserve = fn
	p.mu.Unlock()
}

// Wait 预约下一个可用时刻并等待到那时。nil Pacer 不限速。
func (p *Pacer) Wait(ctx context.Context) error {
	if p == nil {
		return ctx.Err()
	}
	p.mu.Lock()
	now := p.now()
	slot := now
	if !p.last.IsZero() {
		gap := p.minGap
		if p.maxGap > p.minGap {
			gap += time.Duration(p.rng.Int64N(int64(p.maxGap - p.minGap)))
		}
		if t := p.last.Add(gap); t.After(slot) {
			slot = t
		}
	}
	if p.perMinute > 0 {
		p.prune(slot)
		if len(p.window) >= p.perMinute {
			if t := p.window[len(p.window)-p.perMinute].Add(time.Minute); t.After(slot) {
				slot = t
			}
			p.prune(slot)
		}
	}
	p.last = slot
	p.window = append(p.window, slot)
	hook := p.onReserve
	p.mu.Unlock()
	if hook != nil {
		hook(slot)
	}
	return p.sleep(ctx, slot.Sub(now))
}

// prune 丢掉 slot 之前 60 秒以外的记录。
func (p *Pacer) prune(slot time.Time) {
	i := 0
	for i < len(p.window) && !p.window[i].After(slot.Add(-time.Minute)) {
		i++
	}
	p.window = p.window[i:]
}

// Focus 前台锁：同一时刻只有一个标签页处于前台并接收鼠标/键盘输入。
// 后台标签页只做导航和读取页面数据（相当于真人在新标签页打开链接、稍后再看），
// 绝不在不可见的页面上发送合成输入。
type Focus struct {
	sem    chan struct{}
	mu     sync.Mutex
	holder *rod.Page // 当前持有前台的标签页
	active *rod.Page // 浏览器里当前被激活的标签页

	log *slog.Logger
}

func NewFocus(log *slog.Logger) *Focus {
	if log == nil {
		log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	return &Focus{sem: make(chan struct{}, 1), log: log}
}

// visibleCheckEvery 页面不可见时多久重查一次。
const visibleCheckEvery = 5 * time.Second

// minimized 标签页所在的 Chrome 窗口是否被最小化。
func minimized(ctx context.Context, page *rod.Page) bool {
	b := page.Browser().Context(ctx).Timeout(inputTimeout)
	w, err := proto.BrowserGetWindowForTarget{TargetID: page.TargetID}.Call(b)
	return err == nil && w.Bounds != nil && w.Bounds.WindowState == proto.BrowserWindowStateMinimized
}

// BrowserAlive Chrome 是否还连着（进程被关闭、崩溃或调试连接断开时返回 false）。
func BrowserAlive(ctx context.Context, b *rod.Browser) bool {
	_, err := proto.BrowserGetVersion{}.Call(b.Context(ctx).Timeout(inputTimeout))
	return err == nil
}

// waitVisible 等到 page 真正可见。Chrome 窗口被最小化（或被其他窗口完全挡住，且 Chrome 没有关闭遮挡检测）时
// 页面是 hidden：Chrome 不会处理输入事件，而且"不可见页面收到鼠标键盘输入"本身就是自动化特征，
// 所以暂停所有输入，直到窗口恢复。页面加载不受影响。
// 最小化视为用户主动暂停：不去激活标签页（激活会把窗口从最小化里弹出来），等用户自己恢复。
func (f *Focus) waitVisible(ctx context.Context, page *rod.Page, activate func() error) error {
	warned := time.Time{}
	for {
		visible := false
		if !minimized(ctx, page) {
			if err := activate(); err != nil {
				return err
			}
			res, err := page.Context(ctx).Timeout(inputTimeout).Eval(`() => document.visibilityState`)
			visible = err == nil && res.Value.Str() == "visible"
			if err != nil && ctx.Err() == nil && !BrowserAlive(ctx, page.Browser()) {
				return ErrBrowserClosed // 读不到页面状态是因为浏览器没了，不是窗口不可见
			}
		} else if ctx.Err() == nil && !BrowserAlive(ctx, page.Browser()) {
			return ErrBrowserClosed
		}
		if visible {
			if !warned.IsZero() {
				f.log.Info("Chrome 窗口已恢复可见，继续操作", "等待", time.Since(warned).Round(time.Second).String())
			}
			return nil
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if warned.IsZero() {
			warned = time.Now()
			f.log.Warn("Chrome 窗口不可见，暂停鼠标键盘操作。窗口被最小化时请恢复它；想后台静默运行请开启 browser.silent " +
				"（窗口会移到屏幕外，需要 Chrome 带 --disable-features=CalculateNativeWinOcclusion 启动）")
		}
		if err := sleepCtx(ctx, visibleCheckEvery); err != nil {
			return err
		}
	}
}

// Hold 让 page 获得前台（必要时切换标签页），返回释放函数。
// 同一标签页重复 Hold 是可重入的（例如在 Foreground 作用域内调用 Dwell/Scroll）。nil Focus 不做任何事。
func (f *Focus) Hold(ctx context.Context, page *rod.Page, h *Human) (func(), error) {
	if f == nil {
		return func() {}, nil
	}
	f.mu.Lock()
	if f.holder == page {
		f.mu.Unlock()
		return func() {}, nil
	}
	f.mu.Unlock()

	select {
	case f.sem <- struct{}{}:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	release := func() {
		f.mu.Lock()
		f.holder = nil
		f.mu.Unlock()
		<-f.sem
	}
	f.mu.Lock()
	f.holder = page
	f.mu.Unlock()
	activate := func() error {
		f.mu.Lock()
		switchTab := f.active != page
		f.mu.Unlock()
		if !switchTab {
			return nil
		}
		if err := (proto.TargetActivateTarget{TargetID: page.TargetID}).Call(page.Browser().Context(ctx).Timeout(inputTimeout)); err != nil {
			if ctx.Err() == nil && !BrowserAlive(ctx, page.Browser()) {
				return ErrBrowserClosed
			}
			return err
		}
		f.mu.Lock()
		f.active = page
		f.mu.Unlock()
		// 切换标签页后人需要一点时间看清页面
		return h.Sleep(ctx, 400*time.Millisecond, 1500*time.Millisecond)
	}
	if err := f.waitVisible(ctx, page, activate); err != nil {
		release()
		return nil, err
	}
	return release, nil
}

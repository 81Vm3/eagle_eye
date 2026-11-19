package xhs

import (
	"context"
	"math"
	"math/rand/v2"
	"time"

	"github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/input"
	"github.com/go-rod/rod/lib/proto"
	"github.com/ysmood/gson"
)

// Human 模拟真人的停顿、鼠标轨迹、滚动和打字。所有页面交互都走真实的输入事件，
// 不直接调用 DOM 的 click()，也不直接请求接口。
type Human struct {
	rng *rand.Rand
	pos proto.Point // 当前鼠标位置（每个 Human 只服务一个标签页）
}

func NewHuman() *Human {
	return &Human{rng: rand.New(rand.NewPCG(uint64(time.Now().UnixNano()), rand.Uint64()))}
}

// Chance 以概率 p 返回 true。
func (h *Human) Chance(p float64) bool { return p > 0 && h.rng.Float64() < p }

// IntIn 返回 [min, max] 内的随机整数。
func (h *Human) IntIn(min, max int) int {
	if max <= min {
		return min
	}
	return min + h.rng.IntN(max-min+1)
}

// jitter 返回 [min,max] 内偏向较短一端的随机时长（对数正态的近似），更像人的反应时间。
func (h *Human) jitter(min, max time.Duration) time.Duration {
	if max <= min {
		return min
	}
	x := math.Exp(h.rng.NormFloat64()*0.5) / math.E // 大多落在 0.2~0.8
	if x > 1 {
		x = h.rng.Float64()
	}
	return min + time.Duration(float64(max-min)*x)
}

// Sleep 可被 ctx 打断的随机等待。
func (h *Human) Sleep(ctx context.Context, min, max time.Duration) error {
	t := time.NewTimer(h.jitter(min, max))
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// inputTimeout 单条输入命令的超时。页面不可见时 Chrome 不会确认输入事件，没有超时会永久阻塞。
const inputTimeout = 5 * time.Second

// cdp 返回带 ctx 和超时的 page，用来直接发送 CDP 输入命令。
// 注意不能用 page.Context(ctx).Mouse：rod 的 Mouse/Keyboard 绑定在原 page 上，不会继承 ctx。
func cdp(ctx context.Context, page *rod.Page) *rod.Page {
	return page.Context(ctx).Timeout(inputTimeout)
}

func viewport(ctx context.Context, page *rod.Page) (w, hgt float64) {
	w, hgt = 1280, 800
	if res, err := cdp(ctx, page).Eval(`() => [window.innerWidth, window.innerHeight]`); err == nil {
		arr := res.Value.Arr()
		if len(arr) == 2 {
			w, hgt = arr[0].Num(), arr[1].Num()
		}
	}
	return
}

func (h *Human) mouse(ctx context.Context, page *rod.Page, ev proto.InputDispatchMouseEvent) error {
	ev.X, ev.Y = h.pos.X, h.pos.Y
	return ev.Call(cdp(ctx, page))
}

// MoveTo 沿直线分多步把鼠标移到目标点，附带少量抖动。
func (h *Human) MoveTo(ctx context.Context, page *rod.Page, x, y float64) error {
	x += float64(h.IntIn(-2, 2))
	y += float64(h.IntIn(-2, 2))
	from := h.pos
	steps := h.IntIn(12, 28)
	for i := 1; i <= steps; i++ {
		h.pos = proto.Point{X: from.X + (x-from.X)*float64(i)/float64(steps), Y: from.Y + (y-from.Y)*float64(i)/float64(steps)}
		if err := h.mouse(ctx, page, proto.InputDispatchMouseEvent{Type: proto.InputDispatchMouseEventTypeMouseMoved}); err != nil {
			return err
		}
	}
	return nil
}

// Click 滚动到可见 → 移动鼠标到元素内随机点 → 短暂停顿 → 点击。
// el 可能是很早以前用带超时的 page 找到的，这里重新绑定 ctx 和超时，避免沿用已经过期的超时。
func (h *Human) Click(ctx context.Context, page *rod.Page, el *rod.Element) error {
	el = el.Context(ctx)
	if err := el.Timeout(inputTimeout).ScrollIntoView(); err != nil {
		return err
	}
	if err := h.Sleep(ctx, 200*time.Millisecond, 700*time.Millisecond); err != nil {
		return err
	}
	shape, err := el.Timeout(inputTimeout).Shape()
	if err != nil {
		return err
	}
	box := shape.Box()
	x := box.X + box.Width*(0.3+0.4*h.rng.Float64())
	y := box.Y + box.Height*(0.3+0.4*h.rng.Float64())
	if err := h.MoveTo(ctx, page, x, y); err != nil {
		return err
	}
	if err := h.Sleep(ctx, 80*time.Millisecond, 350*time.Millisecond); err != nil {
		return err
	}
	btn := proto.InputMouseButtonLeft
	if err := h.mouse(ctx, page, proto.InputDispatchMouseEvent{Type: proto.InputDispatchMouseEventTypeMousePressed,
		Button: btn, Buttons: gson.Int(1), ClickCount: 1}); err != nil {
		return err
	}
	if err := h.Sleep(ctx, 40*time.Millisecond, 120*time.Millisecond); err != nil {
		return err
	}
	return h.mouse(ctx, page, proto.InputDispatchMouseEvent{Type: proto.InputDispatchMouseEventTypeMouseReleased,
		Button: btn, Buttons: gson.Int(0), ClickCount: 1})
}

// Wheel 在当前鼠标位置分几步滚动 dy 像素。
func (h *Human) Wheel(ctx context.Context, page *rod.Page, dy float64, steps int) error {
	for i := 0; i < steps; i++ {
		if err := h.mouse(ctx, page, proto.InputDispatchMouseEvent{Type: proto.InputDispatchMouseEventTypeMouseWheel,
			DeltaY: dy / float64(steps)}); err != nil {
			return err
		}
		if err := h.Sleep(ctx, 10*time.Millisecond, 40*time.Millisecond); err != nil {
			return err
		}
	}
	return nil
}

// Scroll 用鼠标滚轮向下滚一屏左右，偶尔回滚一点。
func (h *Human) Scroll(ctx context.Context, page *rod.Page) error {
	w, hgt := viewport(ctx, page)
	if err := h.MoveTo(ctx, page, w*(0.35+0.3*h.rng.Float64()), hgt*(0.4+0.3*h.rng.Float64())); err != nil {
		return err
	}
	if err := h.Wheel(ctx, page, hgt*(0.55+0.5*h.rng.Float64()), h.IntIn(6, 14)); err != nil {
		return err
	}
	if h.Chance(0.15) {
		if err := h.Sleep(ctx, 400*time.Millisecond, 1200*time.Millisecond); err != nil {
			return err
		}
		if err := h.Wheel(ctx, page, -hgt*0.2*h.rng.Float64(), h.IntIn(3, 6)); err != nil {
			return err
		}
	}
	return nil
}

// Type 逐字输入（InsertText 等价于输入法上屏，中文也适用）。
func (h *Human) Type(ctx context.Context, page *rod.Page, text string) error {
	for _, r := range text {
		if err := (proto.InputInsertText{Text: string(r)}).Call(cdp(ctx, page)); err != nil {
			return err
		}
		if err := h.Sleep(ctx, 90*time.Millisecond, 380*time.Millisecond); err != nil {
			return err
		}
	}
	return nil
}

// Press 按下并松开一个键。
func (h *Human) Press(ctx context.Context, page *rod.Page, key input.Key) error {
	if err := key.Encode(proto.InputDispatchKeyEventTypeKeyDown, 0).Call(cdp(ctx, page)); err != nil {
		return err
	}
	if err := h.Sleep(ctx, 50*time.Millisecond, 150*time.Millisecond); err != nil {
		return err
	}
	return key.Encode(proto.InputDispatchKeyEventTypeKeyUp, 0).Call(cdp(ctx, page))
}

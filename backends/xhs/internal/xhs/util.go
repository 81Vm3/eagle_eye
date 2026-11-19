package xhs

import (
	"context"
	"time"

	"github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/proto"
)

// inputTimeout 单条 CDP 命令（窗口/标签页状态查询）的超时。
const inputTimeout = 5 * time.Second

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

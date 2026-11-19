package xhs

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/launcher"
	"github.com/go-rod/rod/lib/proto"
	"github.com/ysmood/gson"

	"eagle-eye/xhs/internal/config"
)

// Session 持有浏览器和工作标签页。
type Session struct {
	Browser *rod.Browser
	Page    *rod.Page
	close   func()

	hidden *proto.BrowserBounds // 静默模式下窗口原来的位置，退出时恢复
}

// Close 关闭本程序打开的标签页（接管模式下不关浏览器）。
func (s *Session) Close() {
	s.restoreWindow()
	s.close()
}

// offscreen 窗口移到的位置：远离任何显示器（Chrome 会把坐标限制在 ±16384 以内）。
const offscreen = -16384

// Hide 静默模式：把 Chrome 窗口移到屏幕外。窗口保持"正常"状态（不是最小化），配合
// --disable-features=CalculateNativeWinOcclusion，Chrome 仍认为页面可见，鼠标键盘事件照常处理，
// 指纹与有界面运行完全一致，只是你看不到它。
func (s *Session) Hide(ctx context.Context) error {
	b := s.Browser.Context(ctx).Timeout(10 * time.Second)
	w, err := proto.BrowserGetWindowForTarget{TargetID: s.Page.TargetID}.Call(b)
	if err != nil {
		return fmt.Errorf("获取 Chrome 窗口: %w", err)
	}
	orig := *w.Bounds
	if orig.WindowState != proto.BrowserWindowStateNormal {
		// 最小化/最大化/全屏的窗口不能直接改位置，先恢复成普通窗口
		if err := (proto.BrowserSetWindowBounds{WindowID: w.WindowID,
			Bounds: &proto.BrowserBounds{WindowState: proto.BrowserWindowStateNormal}}).Call(b); err != nil {
			return fmt.Errorf("恢复窗口状态: %w", err)
		}
		if w, err = (proto.BrowserGetWindowForTarget{TargetID: s.Page.TargetID}).Call(b); err != nil {
			return err
		}
		orig = *w.Bounds
	}
	// 上次被强杀没来得及恢复时，窗口本来就在屏幕外：退出时放回一个正常位置
	if orig.Left != nil && (*orig.Left <= offscreen/2 || *orig.Left >= -offscreen/2) {
		orig.Left, orig.Top = gson.Int(100), gson.Int(100)
	}
	if err := (proto.BrowserSetWindowBounds{WindowID: w.WindowID,
		Bounds: &proto.BrowserBounds{Left: gson.Int(offscreen), Top: gson.Int(0)}}).Call(b); err != nil {
		return fmt.Errorf("移动窗口: %w", err)
	}
	s.hidden = &proto.BrowserBounds{Left: orig.Left, Top: orig.Top}

	// 自检：移出屏幕后页面必须仍是可见的，否则鼠标键盘操作会全部暂停
	if _, err := s.Page.Context(ctx).Timeout(5 * time.Second).Activate(); err != nil {
		return err
	}
	visible := false
	for i := 0; i < 6 && !visible; i++ { // 最多等 3 秒
		time.Sleep(500 * time.Millisecond)
		res, err := s.Page.Context(ctx).Timeout(5 * time.Second).Eval(`() => document.visibilityState`)
		visible = err == nil && res.Value.Str() == "visible"
	}
	if !visible {
		s.restoreWindow()
		return fmt.Errorf("静默模式自检失败：窗口移到屏幕外后页面变成了不可见。" +
			"这个 Chrome 启动时没带 --disable-features=CalculateNativeWinOcclusion，" +
			"请完全关闭专用 Chrome（xhs-chrome），让程序按 cdp_launch 重新启动它")
	}
	return nil
}

// restoreWindow 把静默模式移走的窗口放回原位。
func (s *Session) restoreWindow() {
	if s.hidden == nil {
		return
	}
	b := s.Browser.Timeout(5 * time.Second)
	if w, err := (proto.BrowserGetWindowForTarget{TargetID: s.Page.TargetID}).Call(b); err == nil {
		_ = proto.BrowserSetWindowBounds{WindowID: w.WindowID, Bounds: s.hidden}.Call(b)
	}
	s.hidden = nil
}

// OpenBrowser 打开浏览器。两种模式：
//
//  1. cdp_url 非空：接管一个已经在运行的 Chrome（你日常使用、已登录的那个），
//     只新开一个标签页，退出时只关这个标签页，不关浏览器。
//  2. 否则：用固定的 user_data_dir 启动一个有界面的 Chrome，登录态保存在这个目录里，
//     与普通用户“一直用同一个浏览器”的形态一致；不注入 cookie、不伪造指纹。
func OpenBrowser(cfg config.BrowserConfig) (*Session, error) {
	if cfg.CDPURL != "" {
		u, err := launcher.ResolveURL(cfg.CDPURL)
		if err != nil && len(cfg.CDPLaunch) > 0 {
			u, err = launchForCDP(cfg)
		}
		if err != nil {
			return nil, fmt.Errorf("连接 cdp_url %s（Chrome 是否已用 --remote-debugging-port 启动？）: %w", cfg.CDPURL, err)
		}
		// NoDefaultDevice：rod 默认会模拟一台设备并改写 UA/视口，
		// 那会让指纹前后不一致，必须关掉。
		b := rod.New().ControlURL(u).NoDefaultDevice()
		if err := b.Connect(); err != nil {
			return nil, fmt.Errorf("连接 Chrome(%s): %w", cfg.CDPURL, err)
		}
		page, err := b.Page(proto.TargetCreateTarget{URL: "about:blank"})
		if err != nil {
			return nil, err
		}
		return &Session{Browser: b, Page: page, close: func() { _ = page.Close() }}, nil
	}

	return launchLocal(cfg)
}

// launchForCDP 执行 cdp_launch 启动 Chrome（例如 WSL 中启动 Windows 的 chrome.exe），
// 并等待调试端口就绪。Chrome 独立于本程序运行，程序退出后不会被关闭。
func launchForCDP(cfg config.BrowserConfig) (string, error) {
	cmd := exec.Command(cfg.CDPLaunch[0], cfg.CDPLaunch[1:]...)
	if err := cmd.Start(); err != nil {
		return "", fmt.Errorf("启动 Chrome: %w", err)
	}
	go func() { _ = cmd.Wait() }()

	var lastErr error
	for i := 0; i < 30; i++ {
		time.Sleep(time.Second)
		u, err := launcher.ResolveURL(cfg.CDPURL)
		if err == nil {
			return u, nil
		}
		lastErr = err
	}
	return "", fmt.Errorf("Chrome 已启动但调试端口 30 秒内未就绪"+
		"（如果该 user-data-dir 的 Chrome 已经在运行且没开调试端口，请先完全关闭它）: %w", lastErr)
}

func launchLocal(cfg config.BrowserConfig) (*Session, error) {
	dir, err := filepath.Abs(cfg.UserDataDir)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}

	l := launcher.New().
		UserDataDir(dir).
		Headless(cfg.Headless).
		Set("window-size", "1440,900").
		Set("lang", "zh-CN").
		Set("disable-blink-features", "AutomationControlled").
		// 窗口被其他窗口挡住时 Windows 会把页面判成不可见，关掉遮挡检测；最小化仍然不可见
		Set("disable-features", "CalculateNativeWinOcclusion").
		Set("disable-backgrounding-occluded-windows").
		Delete("enable-automation")
	if cfg.ChromePath != "" {
		l = l.Bin(cfg.ChromePath)
	} else if p, ok := launcher.LookPath(); ok {
		l = l.Bin(p)
	}
	if cfg.Proxy != "" {
		l = l.Proxy(cfg.Proxy)
	}
	u, err := l.Launch()
	if err != nil {
		return nil, fmt.Errorf("启动 Chrome: %w", err)
	}
	b := rod.New().ControlURL(u).NoDefaultDevice()
	if err := b.Connect(); err != nil {
		l.Kill()
		return nil, err
	}

	// 复用已有的第一个标签页，没有再新建。
	var page *rod.Page
	if pages, err := b.Pages(); err == nil && len(pages) > 0 {
		page = pages.First()
	} else if page, err = b.Page(proto.TargetCreateTarget{URL: "about:blank"}); err != nil {
		l.Kill()
		return nil, err
	}
	return &Session{Browser: b, Page: page, close: func() {
		_ = b.Close()
		l.Cleanup()
	}}, nil
}

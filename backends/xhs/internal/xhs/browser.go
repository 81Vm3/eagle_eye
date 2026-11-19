package xhs

import (
	"context"
	"fmt"
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
	// Headless 本程序启动的无头 Chrome：没有窗口，静默移窗、最小化/可见性检测都不适用
	Headless bool
	close    func()

	// ua 无头模式下每个工作标签页都要套用的 UA 覆盖（nil = 不改写）
	ua *proto.EmulationSetUserAgentOverride

	hidden *proto.BrowserBounds // 静默模式下窗口原来的位置，退出时恢复
}

// Close 关闭本程序打开的标签页（接管模式下不关浏览器）。
func (s *Session) Close() {
	s.restoreWindow()
	s.close()
}

// NewPage 新开一个工作标签页（接管模式下在后台打开，不抢用户窗口的焦点），无头模式下套用与主标签页相同的 UA 覆盖。
// 返回的 page 不绑定 ctx（ctx 取消后仍能关闭它）；调用方用完必须 ClosePage。
func (s *Session) NewPage(ctx context.Context) (*rod.Page, error) {
	page, err := s.Browser.Context(ctx).Page(proto.TargetCreateTarget{URL: "about:blank", Background: !s.Headless})
	if err != nil {
		return nil, err
	}
	page = page.Context(context.Background())
	if s.ua != nil {
		if err := s.ua.Call(page.Context(ctx).Timeout(10 * time.Second)); err != nil {
			ClosePage(page)
			return nil, fmt.Errorf("设置 User-Agent: %w", err)
		}
	}
	return page, nil
}

// ClosePage 关闭 NewPage 打开的标签页（不受调用方 ctx 影响，最多等 5 秒）。
func ClosePage(page *rod.Page) {
	_ = page.Timeout(5 * time.Second).Close()
}

// offscreen 窗口移到的位置：远离任何显示器（Chrome 会把坐标限制在 ±16384 以内）。
const offscreen = -16384

// isOffscreen 窗口左上角是否已在屏幕外（远离任何显示器）。
func isOffscreen(left *int) bool {
	return left != nil && (*left <= offscreen/2 || *left >= -offscreen/2)
}

// occlusionFlags 自检失败时提示的启动参数。Chrome 可能与本程序不在同一系统（WSL 里接管 Windows 的 Chrome），
// 所以不按本程序的 GOOS 判断，两种都列出来。
const occlusionFlags = "Windows 需要 --disable-features=CalculateNativeWinOcclusion，" +
	"所有平台都需要 --disable-backgrounding-occluded-windows（eagle-xhs init 生成的 cdp_launch 已包含）"

// Hide 静默模式：把 Chrome 窗口移到屏幕外。窗口保持"正常"状态（不是最小化），配合关闭遮挡检测的启动参数，
// Chrome 仍认为页面可见，鼠标键盘事件照常处理，指纹与有界面运行完全一致，只是你看不到它。
//
// 返回 hidden=false、err=nil 表示窗口管理器把窗口约束回了屏幕内（macOS 可能这样做）：
// 窗口仍然可见、照常工作，只是没有隐藏，调用方应提示用户关闭 silent。
func (s *Session) Hide(ctx context.Context) (hidden bool, err error) {
	b := s.Browser.Context(ctx).Timeout(10 * time.Second)
	w, err := proto.BrowserGetWindowForTarget{TargetID: s.Page.TargetID}.Call(b)
	if err != nil {
		return false, fmt.Errorf("获取 Chrome 窗口: %w", err)
	}
	orig := *w.Bounds
	if orig.WindowState != proto.BrowserWindowStateNormal {
		// 最小化/最大化/全屏的窗口不能直接改位置，先恢复成普通窗口
		if err := (proto.BrowserSetWindowBounds{WindowID: w.WindowID,
			Bounds: &proto.BrowserBounds{WindowState: proto.BrowserWindowStateNormal}}).Call(b); err != nil {
			return false, fmt.Errorf("恢复窗口状态: %w", err)
		}
		if w, err = (proto.BrowserGetWindowForTarget{TargetID: s.Page.TargetID}).Call(b); err != nil {
			return false, err
		}
		orig = *w.Bounds
	}
	// 上次被强杀没来得及恢复时，窗口本来就在屏幕外：退出时放回一个正常位置
	if isOffscreen(orig.Left) {
		orig.Left, orig.Top = gson.Int(100), gson.Int(100)
	}
	if err := (proto.BrowserSetWindowBounds{WindowID: w.WindowID,
		Bounds: &proto.BrowserBounds{Left: gson.Int(offscreen), Top: gson.Int(0)}}).Call(b); err != nil {
		return false, fmt.Errorf("移动窗口: %w", err)
	}
	s.hidden = &proto.BrowserBounds{Left: orig.Left, Top: orig.Top}

	// 窗口管理器可能不允许窗口完全离开屏幕（macOS 会把窗口约束回可见区域）：读回实际位置
	if now, err := (proto.BrowserGetWindowForTarget{TargetID: s.Page.TargetID}).Call(b); err == nil &&
		now.Bounds != nil && now.Bounds.Left != nil && !isOffscreen(now.Bounds.Left) {
		s.restoreWindow()
		return false, nil
	}

	// 自检：移出屏幕后页面必须仍是可见的，否则鼠标键盘操作会全部暂停
	if _, err := s.Page.Context(ctx).Timeout(5 * time.Second).Activate(); err != nil {
		return false, err
	}
	visible := false
	for i := 0; i < 6 && !visible; i++ { // 最多等 3 秒
		time.Sleep(500 * time.Millisecond)
		res, err := s.Page.Context(ctx).Timeout(5 * time.Second).Eval(`() => document.visibilityState`)
		visible = err == nil && res.Value.Str() == "visible"
	}
	if !visible {
		s.restoreWindow()
		return false, fmt.Errorf("静默模式自检失败：窗口移到屏幕外后页面变成了不可见，"+
			"这个 Chrome 启动时缺少关闭遮挡检测的参数（%s）。"+
			"请完全关闭专用 Chrome，让程序按 cdp_launch 重新启动它，或关闭 browser.silent", occlusionFlags)
	}
	return true, nil
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

// OpenBrowser 打开 channel search 用的浏览器。三种模式：
//
//  1. headless=true（默认）：用专用 profile（user_data_dir）启动 --headless=new 的 Chrome，
//     UA 换成同版本有界面 Chrome 的值，用完关闭 Chrome；不需要 cdp_url。
//  2. cdp_url 非空：接管一个已经在运行的 Chrome（eagle xhs browser 启动的专用 Chrome），
//     只新开一个标签页，退出时只关这个标签页，不关浏览器。
//  3. 否则：用固定的 user_data_dir 启动一个有界面的 Chrome，用完关闭。
//
// 都不注入 cookie、不伪造指纹（无头模式只把 UA 里的 HeadlessChrome 换回 Chrome）。
func OpenBrowser(cfg config.BrowserConfig) (*Session, error) {
	if cfg.Headless {
		return launchLocal(cfg, true)
	}
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
	return launchLocal(cfg, false)
}

// LoginConfig login 用的浏览器配置：扫码必须有界面，所以永远不是无头，也不静默移窗。
//   - headless=true：不接管 cdp_url，用同一个专用 profile（user_data_dir）启动有界面 Chrome，
//     无头搜索与 login 共用这个 profile 才能共享登录态；
//   - headless=false：保持原行为（有 cdp_url 时在接管的 Chrome 里新开标签页，否则用 user_data_dir 启动有界面 Chrome）。
func LoginConfig(cfg config.BrowserConfig) config.BrowserConfig {
	if cfg.Headless {
		cfg.CDPURL, cfg.CDPLaunch = "", nil
	}
	cfg.Headless, cfg.Silent = false, false
	return cfg
}

// OpenLogin 打开 login 用的有界面浏览器（见 LoginConfig）。结束时 Session.Close 关闭本程序启动的窗口。
func OpenLogin(cfg config.BrowserConfig) (*Session, error) {
	return OpenBrowser(LoginConfig(cfg))
}

// launchForCDP 执行 cdp_launch 启动 Chrome（例如 WSL 中启动 Windows 的 chrome.exe），
// 并等待调试端口就绪。Chrome 独立于本程序运行，程序退出后不会被关闭。
func launchForCDP(cfg config.BrowserConfig) (string, error) {
	if err := StartChrome(context.Background(), cfg.CDPLaunch, cfg.CDPURL, 30*time.Second); err != nil {
		return "", err
	}
	return launcher.ResolveURL(cfg.CDPURL)
}

package xhs

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/launcher"
	"github.com/go-rod/rod/lib/proto"

	"eagle-eye/xhs/internal/config"
	"eagle-eye/xhs/internal/setup"
)

// WindowSize 本程序自行启动的 Chrome 的窗口大小（headless 与 login 一致：常见的笔记本分辨率）。
const WindowSize = "1440,900"

// ErrProfileInUse 专用 profile 正被另一个 Chrome 进程使用。
var ErrProfileInUse = errors.New("专用 Chrome profile 正被另一个 Chrome 使用")

// ErrNoChrome 本机找不到 Chrome。
var ErrNoChrome = errors.New("没有在本机找到 Chrome / Chromium / Edge")

// FindLocalChrome 本程序自行启动 Chrome（headless 搜索、login）时用的可执行文件：
// browser.chrome_path > 本平台默认位置（与 eagle-xhs init 相同的查找顺序）> rod 的查找。
// 不使用 cdp_launch[0]：它可能是另一个系统上的 Chrome（WSL 里的 chrome.exe），profile 路径对不上。
// 找不到时不自动下载 Chromium（下载慢，而且登录态会落在与日常 Chrome 不同的内核上）。
func FindLocalChrome(cfg config.BrowserConfig, p setup.Platform) (string, error) {
	if cfg.ChromePath != "" {
		return cfg.ChromePath, nil
	}
	if c, ok := setup.FindChrome(p); ok {
		return c, nil
	}
	if c, ok := launcher.LookPath(); ok {
		return c, nil
	}
	return "", fmt.Errorf("%w（查找过 %v）：请安装 Chrome，或在配置里设置 browser.chrome_path", ErrNoChrome, setup.ChromeCandidates(p))
}

// LocalLauncher 本程序用专用 profile 启动 Chrome 的参数。headless=true 用 --headless=new（新版无头，
// 与有界面 Chrome 同一套内核与渲染路径，不用旧 headless），false 为有界面（login）。
// 两种模式都去掉 enable-automation、关掉 AutomationControlled，窗口大小固定。
func LocalLauncher(cfg config.BrowserConfig, bin, profileDir string, headless bool) *launcher.Launcher {
	l := launcher.New().
		Bin(bin).
		UserDataDir(profileDir).
		HeadlessNew(headless).
		Set("window-size", WindowSize).
		Set("lang", "zh-CN").
		Set("disable-blink-features", "AutomationControlled").
		// 窗口被其他窗口挡住时 Windows 会把页面判成不可见，关掉遮挡检测；最小化仍然不可见。
		// 其余三个在所有平台（含 macOS）关掉被遮挡/后台窗口的降级与计时器节流（与 setup.LaunchFlags 一致）
		Set("disable-features", "CalculateNativeWinOcclusion").
		Set("disable-backgrounding-occluded-windows").
		Set("disable-renderer-backgrounding").
		Set("disable-background-timer-throttling").
		Delete("enable-automation")
	if headless {
		// 无头模式默认屏幕是 800x600，与 1440x900 的窗口矛盾：报告一块常见的 1920x1080 屏幕
		// （Chrome 120+ 的 headless 支持该参数，旧版本忽略）
		l = l.Set("screen-info", "{1920x1080}")
	}
	if cfg.Proxy != "" {
		l = l.Proxy(cfg.Proxy)
	}
	return l
}

// launchLocal 用专用 profile（user_data_dir）启动 Chrome。用完 Session.Close 会正常关闭 Chrome
// （等它把登录态写回 profile），绝不删除 profile 目录。
func launchLocal(cfg config.BrowserConfig, headless bool) (*Session, error) {
	dir, err := filepath.Abs(cfg.UserDataDir)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	if pid, busy := ProfileInUse(dir); busy {
		return nil, profileInUseError(dir, pid)
	}
	bin, err := FindLocalChrome(cfg, setup.Host())
	if err != nil {
		return nil, err
	}

	l := LocalLauncher(cfg, bin, dir, headless)
	u, err := l.Launch()
	if err != nil {
		if pid, busy := ProfileInUse(dir); busy {
			return nil, profileInUseError(dir, pid)
		}
		return nil, fmt.Errorf("启动 Chrome（%s）: %w", bin, err)
	}
	// NoDefaultDevice：rod 默认会模拟一台设备并改写 UA/视口，必须关掉
	b := rod.New().ControlURL(u).NoDefaultDevice()
	if err := b.Connect(); err != nil {
		l.Kill()
		return nil, err
	}
	closeAll := func() { closeLocal(b, l) }

	// 复用已有的第一个标签页，没有再新建。
	var page *rod.Page
	if pages, err := b.Pages(); err == nil && len(pages) > 0 {
		page = pages.First()
	} else if page, err = b.Page(proto.TargetCreateTarget{URL: "about:blank"}); err != nil {
		closeAll()
		return nil, err
	}
	var ua *proto.EmulationSetUserAgentOverride
	if headless {
		if ua, err = uaOverride(b, bin); err == nil {
			err = ua.Call(page.Timeout(10 * time.Second))
		}
		if err != nil {
			closeAll()
			return nil, fmt.Errorf("设置 User-Agent: %w", err)
		}
	}
	return &Session{Browser: b, Page: page, Headless: headless, close: closeAll, ua: ua}, nil
}

func profileInUseError(dir string, pid int) error {
	who := ""
	if pid > 0 {
		who = fmt.Sprintf("（pid %d）", pid)
	}
	return fmt.Errorf("%w%s：%s。通常是 eagle xhs browser 启动的有界面专用 Chrome 还开着——"+
		"browser.headless=true 时 channel search / login 会自己启动 Chrome，请先完全关闭那个 Chrome 再重试；"+
		"想继续接管它，把配置里的 browser.headless 改为 false", ErrProfileInUse, who, dir)
}

// closeLocal 正常关闭本程序启动的 Chrome：Browser.close 让 Chrome 自己退出（cookie 等登录态落盘），
// 等进程结束；超时才强杀。不调用 launcher.Cleanup（它会删掉 user-data-dir，也就是登录态）。
func closeLocal(b *rod.Browser, l *launcher.Launcher) {
	pid := l.PID()
	_ = b.Close()
	deadline := time.Now().Add(10 * time.Second)
	for pid > 0 && processAlive(pid) && time.Now().Before(deadline) {
		time.Sleep(200 * time.Millisecond)
	}
	if pid > 0 && processAlive(pid) {
		l.Kill()
	}
}

// ProfileInUse 报告 user-data-dir 是否正被一个仍在运行的 Chrome 使用（返回其 pid，未知时为 0）。
// Chrome 在 unix 上用 SingletonLock 符号链接（指向 "<主机名>-<pid>"），Windows 上独占打开 lockfile。
// 进程已不存在的残留锁（Chrome 被强杀）不算占用，Chrome 启动时会自己清理。
func ProfileInUse(dir string) (int, bool) {
	if target, err := os.Readlink(filepath.Join(dir, "SingletonLock")); err == nil {
		host, pid := parseSingleton(target)
		if pid <= 0 {
			return 0, true
		}
		if h, err := os.Hostname(); err == nil && host != "" && host != h {
			return pid, true // 别的机器上的进程（共享目录）：无法判断，保守地当作占用
		}
		return pid, processAlive(pid)
	}
	return 0, lockfileBusy(filepath.Join(dir, "lockfile"))
}

// parseSingleton 解析 SingletonLock 的目标 "<主机名>-<pid>"（主机名本身可能含 '-'）。
func parseSingleton(target string) (string, int) {
	i := strings.LastIndexByte(target, '-')
	if i < 0 {
		return "", 0
	}
	pid, err := strconv.Atoi(target[i+1:])
	if err != nil {
		return "", 0
	}
	return target[:i], pid
}

// ---------- User-Agent ----------

// NormalUA 把无头 Chrome 的 UA 换成同版本有界面 Chrome 的 UA：只把 "HeadlessChrome/" 换成 "Chrome/"，
// 其余（系统、版本号）都来自浏览器本身。
func NormalUA(ua string) string {
	return strings.ReplaceAll(ua, "HeadlessChrome/", "Chrome/")
}

// productVersion "HeadlessChrome/131.0.6778.85" → "131.0.6778.85"。
func productVersion(product string) string {
	if i := strings.LastIndexByte(product, '/'); i >= 0 {
		return product[i+1:]
	}
	return product
}

// BrandFor 按可执行文件判断品牌（sec-ch-ua 里与 "Chromium" 并列的那一项）：Chromium 没有，Edge 是 "Microsoft Edge"，
// 其余按 Google Chrome。
func BrandFor(bin string) string {
	b := strings.ToLower(filepath.Base(strings.ReplaceAll(bin, `\`, "/")))
	switch {
	case strings.Contains(b, "chromium"):
		return ""
	case strings.Contains(b, "edge") || strings.Contains(b, "msedge"):
		return "Microsoft Edge"
	}
	return "Google Chrome"
}

// BrandList 复刻 Chromium 生成 sec-ch-ua 品牌列表的算法（components/embedder_support/user_agent_utils.cc
// GenerateBrandVersionList）：以主版本号为种子，GREASE 品牌名、版本和三项的顺序都是确定的。
// 有界面 Chrome 发出的就是这个列表；无头 Chrome 把品牌换成了 HeadlessChrome，这里换回去。
// full=true 时版本是完整版本号（fullVersionList），否则只有主版本（brands / sec-ch-ua）。
func BrandList(major int, fullVersion, brand string, full bool) []*proto.EmulationUserAgentBrandVersion {
	greaseChars := []string{" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_"}
	greaseVersions := []string{"8", "99", "24"}
	orders := [6][3]int{{0, 1, 2}, {0, 2, 1}, {1, 0, 2}, {1, 2, 0}, {2, 0, 1}, {2, 1, 0}}
	seed := max(major, 0)
	order := orders[seed%6]

	ver, gver := strconv.Itoa(major), greaseVersions[seed%3]
	if full {
		ver, gver = fullVersion, gver+".0.0.0"
	}
	grease := &proto.EmulationUserAgentBrandVersion{
		Brand:   "Not" + greaseChars[seed%len(greaseChars)] + "A" + greaseChars[(seed+1)%len(greaseChars)] + "Brand",
		Version: gver,
	}
	chromium := &proto.EmulationUserAgentBrandVersion{Brand: "Chromium", Version: ver}
	if brand == "" {
		list := make([]*proto.EmulationUserAgentBrandVersion, 2)
		list[seed%2] = grease
		list[(seed+1)%2] = chromium
		return list
	}
	list := make([]*proto.EmulationUserAgentBrandVersion, 3)
	list[order[0]] = grease
	list[order[1]] = chromium
	list[order[2]] = &proto.EmulationUserAgentBrandVersion{Brand: brand, Version: ver}
	return list
}

// uaPlatform 从 UA 里的系统标识得到 userAgentData.platform（sec-ch-ua-platform）。
func uaPlatform(ua string) string {
	switch {
	case strings.Contains(ua, "Windows"):
		return "Windows"
	case strings.Contains(ua, "Macintosh"):
		return "macOS"
	case strings.Contains(ua, "CrOS"):
		return "Chrome OS"
	case strings.Contains(ua, "Android"):
		return "Android"
	}
	return "Linux"
}

// highEntropy 浏览器自己报告的 navigator.userAgentData（含高熵字段）。
type highEntropy struct {
	Brands          []*proto.EmulationUserAgentBrandVersion `json:"brands"`
	FullVersionList []*proto.EmulationUserAgentBrandVersion `json:"fullVersionList"`
	Platform        string                                  `json:"platform"`
	PlatformVersion string                                  `json:"platformVersion"`
	Architecture    string                                  `json:"architecture"`
	Bitness         string                                  `json:"bitness"`
	Model           string                                  `json:"model"`
	Wow64           bool                                    `json:"wow64"`
}

// realBrands 浏览器报告的品牌列表可以直接用：非空、而且不含 Headless 字样。
func realBrands(list []*proto.EmulationUserAgentBrandVersion) bool {
	if len(list) == 0 {
		return false
	}
	for _, b := range list {
		if b == nil || b.Brand == "" || strings.Contains(b.Brand, "Headless") {
			return false
		}
	}
	return true
}

// UAMetadata 有界面 Chrome 同版本的 userAgentData（sec-ch-ua*）。
// he 是浏览器自己报告的值（见 readHighEntropy）：品牌列表正常（新版 headless 本来就报 "Google Chrome"）时原样使用，
// 含 HeadlessChrome 或读不到时按 Chromium 的算法生成；平台版本、架构等取浏览器真实值，读不到时按本机推断。
func UAMetadata(ua, fullVersion, brand string, he *highEntropy) *proto.EmulationUserAgentMetadata {
	major, _ := strconv.Atoi(strings.SplitN(fullVersion, ".", 2)[0])
	if he == nil {
		he = &highEntropy{Bitness: "64"}
		switch runtime.GOARCH {
		case "arm64", "arm":
			he.Architecture = "arm"
		default:
			he.Architecture = "x86"
		}
	}
	brands, full := he.Brands, he.FullVersionList
	if !realBrands(brands) || !realBrands(full) {
		brands, full = BrandList(major, fullVersion, brand, false), BrandList(major, fullVersion, brand, true)
	}
	platform := he.Platform
	if platform == "" {
		platform = uaPlatform(ua)
	}
	return &proto.EmulationUserAgentMetadata{
		Brands:          brands,
		FullVersionList: full,
		FullVersion:     fullVersion,
		Platform:        platform,
		PlatformVersion: he.PlatformVersion,
		Architecture:    he.Architecture,
		Model:           he.Model,
		Mobile:          false,
		Bitness:         he.Bitness,
		Wow64:           he.Wow64,
	}
}

// uaOverride 计算 UA 覆盖：把工作标签页的 UA（navigator.userAgent、HTTP User-Agent）和 userAgentData（sec-ch-ua*）
// 改成同版本有界面 Chrome 的值。版本、系统都取自浏览器本身（Browser.getVersion），只替换品牌。
// 不改 navigator.platform / Accept-Language（无头时本来就是真实值，与 login 时同一个 profile 一致）。
// 浏览器级只算一次，每个工作标签页（搜索页、并行的详情页）都套用同一个值。
func uaOverride(b *rod.Browser, bin string) (*proto.EmulationSetUserAgentOverride, error) {
	v, err := proto.BrowserGetVersion{}.Call(b.Timeout(10 * time.Second))
	if err != nil {
		return nil, err
	}
	ua := NormalUA(v.UserAgent)
	full := productVersion(v.Product)
	meta := UAMetadata(ua, full, BrandFor(bin), readHighEntropy(b))
	return &proto.EmulationSetUserAgentOverride{UserAgent: ua, UserAgentMetadata: meta}, nil
}

// uaProbeURL 读取 navigator.userAgentData 的本地页面：userAgentData 只在安全上下文里存在，about:blank 不是；
// chrome://version 是浏览器内置页面（安全上下文、不联网）。在临时标签页里读，读完关闭，不影响工作标签页的历史。
const uaProbeURL = "chrome://version"

// readHighEntropy 在临时标签页（uaProbeURL）里读浏览器自己报告的 userAgentData；不可用时返回 nil。
func readHighEntropy(b *rod.Browser) *highEntropy {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	probe, err := b.Context(ctx).Page(proto.TargetCreateTarget{URL: uaProbeURL})
	if err != nil {
		return nil
	}
	defer func() { _ = probe.Close() }()
	if err := probe.WaitLoad(); err != nil {
		return nil
	}
	res, err := probe.Eval(`async () => {
		if (!navigator.userAgentData) return "";
		const v = await navigator.userAgentData.getHighEntropyValues(
			["platformVersion", "architecture", "bitness", "model", "wow64", "fullVersionList"]);
		return JSON.stringify(v);
	}`)
	if err != nil || res.Value.Str() == "" {
		return nil
	}
	var he highEntropy
	if json.Unmarshal([]byte(res.Value.Str()), &he) != nil || he.Architecture == "" {
		return nil
	}
	return &he
}

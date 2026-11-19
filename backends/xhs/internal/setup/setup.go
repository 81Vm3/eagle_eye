// Package setup 平台相关的默认值：查找本机 Chrome、生成 cdp_launch、写入默认 config.yaml（eagle-xhs init）。
//
// 所有函数都通过 Platform 拿到 GOOS / 文件存在性 / PATH / 环境变量，测试可以注入任意平台。
package setup

import (
	"errors"
	"fmt"
	"io/fs"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// DefaultCDPURL init 写入的 browser.cdp_url。
const DefaultCDPURL = "http://127.0.0.1:9222"

// Platform 平台探测的注入点。
type Platform struct {
	GOOS     string
	Exists   func(path string) bool
	LookPath func(name string) (string, error)
	Getenv   func(key string) string
}

// Host 当前进程所在平台。
func Host() Platform {
	return Platform{
		GOOS: runtime.GOOS,
		Exists: func(p string) bool {
			st, err := os.Stat(p)
			return err == nil && !st.IsDir()
		},
		LookPath: exec.LookPath,
		Getenv:   os.Getenv,
	}
}

// ChromeCandidates 按优先级列出本平台 Chrome（及兼容浏览器）的候选路径。
// Linux 返回的是命令名，由 FindChrome 在 PATH 里查找。
func ChromeCandidates(p Platform) []string {
	switch p.GOOS {
	case "darwin":
		app := func(dir, name string) string { return dir + "/" + name + ".app/Contents/MacOS/" + name }
		c := []string{app("/Applications", "Google Chrome")}
		if home := p.Getenv("HOME"); home != "" {
			c = append(c, app(home+"/Applications", "Google Chrome"))
		}
		return append(c, app("/Applications", "Chromium"), app("/Applications", "Microsoft Edge"))
	case "windows":
		env := func(key, def string) string {
			if v := p.Getenv(key); v != "" {
				return v
			}
			return def
		}
		pf := env("ProgramFiles", `C:\Program Files`)
		pf86 := env("ProgramFiles(x86)", `C:\Program Files (x86)`)
		c := []string{
			pf + `\Google\Chrome\Application\chrome.exe`,
			pf86 + `\Google\Chrome\Application\chrome.exe`,
		}
		if local := p.Getenv("LOCALAPPDATA"); local != "" {
			c = append(c, local+`\Google\Chrome\Application\chrome.exe`)
		}
		return append(c,
			pf86+`\Microsoft\Edge\Application\msedge.exe`,
			pf+`\Microsoft\Edge\Application\msedge.exe`)
	default: // linux 及其他 unix
		return []string{"google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"}
	}
}

// FindChrome 返回本平台第一个存在的浏览器；都找不到时返回首选候选和 false（写进配置，提示用户修改）。
func FindChrome(p Platform) (string, bool) {
	cands := ChromeCandidates(p)
	for _, c := range cands {
		if p.GOOS != "darwin" && p.GOOS != "windows" {
			if path, err := p.LookPath(c); err == nil {
				return path, true
			}
			continue
		}
		if p.Exists(c) {
			return c, true
		}
	}
	return cands[0], false
}

// LaunchFlags cdp_launch 里除可执行文件、端口、profile 之外的参数。
//
//   - 遮挡/后台节流：窗口被挡住、移到屏幕外或在后台时，Chrome 默认会把页面判成不可见并节流计时器，
//     页面会变慢甚至停止渲染。三个 --disable-* 在所有平台都关掉这些行为（最小化仍然不可见）。
//   - Windows 另有原生遮挡检测 CalculateNativeWinOcclusion，静默模式（窗口移到屏幕外）必须关掉它。
func LaunchFlags(goos string) []string {
	f := []string{
		"--no-first-run",
		"--no-default-browser-check",
		"--disable-backgrounding-occluded-windows",
		"--disable-renderer-backgrounding",
		"--disable-background-timer-throttling",
	}
	if goos == "windows" {
		f = append(f, "--disable-features=CalculateNativeWinOcclusion")
	}
	return f
}

// Port 从 cdp_url 取调试端口，取不到时为 9222。
func Port(cdpURL string) string {
	if u, err := url.Parse(cdpURL); err == nil && u.Port() != "" {
		return u.Port()
	}
	return "9222"
}

// DefaultLaunch 本平台的 cdp_launch：chrome + 调试端口 + 专用 profile + LaunchFlags。
func DefaultLaunch(goos, chrome, cdpURL, profileDir string) []string {
	return append([]string{chrome,
		"--remote-debugging-port=" + Port(cdpURL),
		"--user-data-dir=" + profileDir,
	}, LaunchFlags(goos)...)
}

// yamlQuote 单引号 YAML 标量：不解释反斜杠（Windows 路径原样保留），单引号写成两个。
func yamlQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }

// DefaultConfig 生成适合 p 平台的默认 config.yaml。profileDir 是专用 Chrome 的 --user-data-dir
// （绝对路径，Chrome 不知道配置文件在哪，不能用相对路径）。返回内容和是否找到了本机浏览器。
func DefaultConfig(p Platform, profileDir string) (string, bool) {
	chrome, found := FindChrome(p)
	var launch strings.Builder
	for _, a := range DefaultLaunch(p.GOOS, chrome, DefaultCDPURL, profileDir) {
		fmt.Fprintf(&launch, "    - %s\n", yamlQuote(a))
	}
	note := ""
	if !found {
		note = "  # 没有在本机找到 Chrome / Chromium / Edge：请安装 Chrome，或把下面第一项改成浏览器可执行文件的路径\n"
	}
	return fmt.Sprintf(`# eagle-xhs 配置（由 eagle-xhs init 为 %[1]s 生成；可以手动修改，init 不会覆盖已有文件）。
# 相对路径（db_path / user_data_dir）都相对本配置文件所在目录。

db_path: data/xhs.db        # 风控冷却等跨进程状态

browser:
  # true（默认）：channel search 自己用专用 profile（user_data_dir）启动无头 Chrome（--headless=new），
  #   不弹窗口、用完关闭，不需要 eagle xhs browser，也不用 cdp_url / cdp_launch。
  # false：接管 cdp_url 上已在运行的有界面专用 Chrome（eagle xhs browser 启动它），新开标签页工作。
  # 两种模式下 eagle xhs login 都打开有界面的 Chrome 扫码，登录态保存在同一个专用 profile 里。
  headless: true
  user_data_dir: data/chrome-profile   # 专用 profile（登录态保存在这里），与下面 --user-data-dir 是同一个目录
  chrome_path: ""                      # 程序自行启动 Chrome（无头搜索 / login）时用的浏览器；空 = 自动查找本机 Chrome
  # 以下只在 headless: false 或调试时使用（eagle xhs browser）：调试端口上的专用 Chrome 与启动它的命令
  cdp_url: %[2]s
%[3]s  cdp_launch:
%[4]s  # 静默运行（headless: false 时把窗口移到屏幕外）：只在 Windows 上验证过；macOS 可能把窗口拉回屏幕内，默认关闭
  silent: false
  proxy: ""

behavior:
  cooldown_hours: 6              # 触发验证码后的冷却时间（连续触发翻倍，上限 48h）

channel:
  timeout_seconds: 120          # 单次搜索总时限，应小于 eagle.toml 里的 timeout_seconds
  max_scrolls: 5                # 搜索结果页最多向下滚动几次
  detail_concurrency: 3         # detail 时最多同时打开几个笔记标签页（1–10）
`, p.GOOS, DefaultCDPURL, note, launch.String()), found
}

// Result Init 的结果。
type Result struct {
	Created     bool   // false = 目标已存在，未改动
	Chrome      string // 写入 cdp_launch 的浏览器
	ChromeFound bool
}

// Init 在 path 写入默认配置；path 已存在时不覆盖（Created=false，不报错）。
func Init(path string, p Platform) (Result, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return Result{}, err
	}
	if _, err := os.Lstat(abs); err == nil {
		return Result{}, nil
	} else if !errors.Is(err, fs.ErrNotExist) {
		return Result{}, err
	}
	dir := filepath.Dir(abs)
	content, found := DefaultConfig(p, filepath.Join(dir, "data", "chrome-profile"))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return Result{}, err
	}
	// O_EXCL：并发 init 时也绝不覆盖别人刚写好的文件
	f, err := os.OpenFile(abs, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if errors.Is(err, fs.ErrExist) {
		return Result{}, nil
	}
	if err != nil {
		return Result{}, err
	}
	if _, err := f.WriteString(content); err != nil {
		f.Close()
		os.Remove(abs)
		return Result{}, err
	}
	if err := f.Close(); err != nil {
		os.Remove(abs)
		return Result{}, err
	}
	chrome, _ := FindChrome(p)
	return Result{Created: true, Chrome: chrome, ChromeFound: found}, nil
}

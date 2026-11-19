package xhs

import (
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/go-rod/rod/lib/proto"

	"eagle-eye/xhs/internal/config"
	"eagle-eye/xhs/internal/setup"
)

func TestLocalLauncherHeadlessArgs(t *testing.T) {
	cfg := config.BrowserConfig{Proxy: "socks5://127.0.0.1:1080"}
	args := LocalLauncher(cfg, "/x/chrome", "/p/profile", true).FormatArgs()
	for _, want := range []string{"--headless=new", "--window-size=" + WindowSize, "--screen-info={1920x1080}",
		"--user-data-dir=/p/profile", "--disable-blink-features=AutomationControlled", "--lang=zh-CN",
		"--proxy-server=socks5://127.0.0.1:1080", "--disable-features=CalculateNativeWinOcclusion"} {
		if !slices.Contains(args, want) {
			t.Errorf("无头参数缺少 %s: %q", want, args)
		}
	}
	for _, a := range args {
		if a == "--headless" || a == "--headless=old" || strings.HasPrefix(a, "--enable-automation") {
			t.Errorf("不应出现 %s", a)
		}
	}

	// login：有界面，不带任何 --headless，同一个 profile
	args = LocalLauncher(config.BrowserConfig{}, "/x/chrome", "/p/profile", false).FormatArgs()
	for _, a := range args {
		if strings.HasPrefix(a, "--headless") || strings.HasPrefix(a, "--enable-automation") || strings.HasPrefix(a, "--screen-info") {
			t.Errorf("有界面不应带 %s", a)
		}
	}
	if !slices.Contains(args, "--user-data-dir=/p/profile") || !slices.Contains(args, "--window-size="+WindowSize) {
		t.Errorf("%q", args)
	}
}

func TestLoginConfigAlwaysHeadful(t *testing.T) {
	base := config.BrowserConfig{CDPURL: "http://127.0.0.1:9222", CDPLaunch: []string{"chrome"}, UserDataDir: "/p", Silent: true}

	// headless=true：不接管 cdp_url，用同一个专用 profile 启动有界面 Chrome
	h := base
	h.Headless = true
	got := LoginConfig(h)
	if got.Headless || got.Silent || got.CDPURL != "" || got.CDPLaunch != nil || got.UserDataDir != "/p" {
		t.Fatalf("%+v", got)
	}
	// headless=false：保持原行为（接管 cdp_url），但仍然有界面、不静默
	got = LoginConfig(base)
	if got.Headless || got.Silent || got.CDPURL != base.CDPURL || len(got.CDPLaunch) != 1 {
		t.Fatalf("%+v", got)
	}
	if h.CDPURL == "" || !h.Headless {
		t.Fatal("不应改动调用方的配置")
	}
}

func TestNormalUA(t *testing.T) {
	in := "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/131.0.0.0 Safari/537.36"
	want := "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
	if got := NormalUA(in); got != want {
		t.Fatalf("%s", got)
	}
	if NormalUA(want) != want {
		t.Fatal("正常 UA 不应改变")
	}
	if productVersion("HeadlessChrome/131.0.6778.85") != "131.0.6778.85" {
		t.Fatal("productVersion")
	}
}

func brands(major int, brand string) string {
	var s []string
	for _, b := range BrandList(major, strconv.Itoa(major)+".0.1.2", brand, false) {
		s = append(s, `"`+b.Brand+`";v="`+b.Version+`"`)
	}
	return strings.Join(s, ", ")
}

// 期望值是对应版本有界面 Chrome / Chromium 实际发出的 sec-ch-ua。
func TestBrandListMatchesChrome(t *testing.T) {
	for major, want := range map[int]string{
		120: `"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"`,
		124: `"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"`,
		131: `"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"`,
		138: `"Not)A;Brand";v="8", "Chromium";v="138", "Google Chrome";v="138"`,
		// 本机 Chrome 157 --headless=new 在 chrome://version 上实际报告的列表
		157: `"Not-A.Brand";v="99", "Google Chrome";v="157", "Chromium";v="157"`,
	} {
		if got := brands(major, "Google Chrome"); got != want {
			t.Errorf("%d:\n got %s\nwant %s", major, got, want)
		}
	}
	if got := brands(131, ""); got != `"Chromium";v="131", "Not_A Brand";v="24"` {
		t.Errorf("Chromium 131: %s", got)
	}
	full := BrandList(131, "131.0.6778.85", "Google Chrome", true)
	if full[0].Version != "131.0.6778.85" || full[2].Version != "24.0.0.0" {
		t.Errorf("fullVersionList: %+v %+v", full[0], full[2])
	}
}

func TestBrandForAndMetadata(t *testing.T) {
	for bin, want := range map[string]string{
		"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome": "Google Chrome",
		"/usr/bin/google-chrome-stable":                                "Google Chrome",
		"/usr/bin/chromium-browser":                                    "",
		`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`: "Microsoft Edge",
	} {
		if got := BrandFor(bin); got != want {
			t.Errorf("%s → %q", bin, got)
		}
	}
	ua := NormalUA("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/131.0.0.0 Safari/537.36")
	m := UAMetadata(ua, "131.0.6778.85", "Google Chrome", &highEntropy{PlatformVersion: "15.0.0", Architecture: "x86", Bitness: "64"})
	if m.Platform != "Windows" || m.PlatformVersion != "15.0.0" || m.Mobile || m.FullVersion != "131.0.6778.85" ||
		len(m.Brands) != 3 || m.Brands[0].Brand != "Google Chrome" {
		t.Fatalf("%+v", m)
	}
	for _, b := range m.Brands {
		if strings.Contains(b.Brand, "Headless") {
			t.Fatal(b.Brand)
		}
	}
	if uaPlatform("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)") != "macOS" || uaPlatform("(X11; Linux x86_64)") != "Linux" {
		t.Fatal("uaPlatform")
	}
	if m := UAMetadata(ua, "131.0.6778.85", "", nil); m.Bitness != "64" || m.Architecture == "" || len(m.Brands) != 2 {
		t.Fatalf("%+v", m)
	}
}

func TestUAMetadataPrefersRealBrands(t *testing.T) {
	ua := "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
	bv := func(b, v string) *proto.EmulationUserAgentBrandVersion {
		return &proto.EmulationUserAgentBrandVersion{Brand: b, Version: v}
	}
	real := &highEntropy{
		Brands:          []*proto.EmulationUserAgentBrandVersion{bv("Not-A.Brand", "99"), bv("Google Chrome", "157"), bv("Chromium", "157")},
		FullVersionList: []*proto.EmulationUserAgentBrandVersion{bv("Not-A.Brand", "99.0.0.0"), bv("Google Chrome", "157.0.8103.79"), bv("Chromium", "157.0.8103.79")},
		Platform:        "Windows", PlatformVersion: "19.0.0", Architecture: "x86", Bitness: "64",
	}
	m := UAMetadata(ua, "157.0.8103.79", "Google Chrome", real)
	if m.Brands[1].Brand != "Google Chrome" || m.FullVersionList[2].Version != "157.0.8103.79" || m.PlatformVersion != "19.0.0" {
		t.Fatalf("%+v", m)
	}
	// 旧版 headless 报 HeadlessChrome：换成按算法生成的列表，平台版本仍用真实值
	real.Brands[1] = bv("HeadlessChrome", "157")
	m = UAMetadata(ua, "157.0.8103.79", "Google Chrome", real)
	if got := m.Brands[0].Brand + "|" + m.Brands[1].Brand + "|" + m.Brands[2].Brand; got != "Not-A.Brand|Google Chrome|Chromium" ||
		m.PlatformVersion != "19.0.0" {
		t.Fatalf("%s %+v", got, m)
	}
}

func TestProfileInUse(t *testing.T) {
	if host, pid := parseSingleton("my-host-name-1234"); host != "my-host-name" || pid != 1234 {
		t.Fatal(host, pid)
	}
	dir := t.TempDir()
	if _, busy := ProfileInUse(dir); busy {
		t.Fatal("空 profile 不应被占用")
	}
	if err := os.Symlink("x", filepath.Join(dir, "probe")); err != nil {
		t.Skip("不支持符号链接")
	}
	host, _ := os.Hostname()
	lock := filepath.Join(dir, "SingletonLock")
	// 本进程还活着：占用
	if err := os.Symlink(host+"-"+strconv.Itoa(os.Getpid()), lock); err != nil {
		t.Fatal(err)
	}
	if pid, busy := ProfileInUse(dir); !busy || pid != os.Getpid() {
		t.Fatalf("应判为占用: %d %v", pid, busy)
	}
	err := profileInUseError(dir, 42)
	if !errors.Is(err, ErrProfileInUse) || !strings.Contains(err.Error(), "eagle xhs browser") || !strings.Contains(err.Error(), "headless") {
		t.Fatal(err)
	}
	// 被强杀留下的残留锁（进程已不存在）：不算占用
	_ = os.Remove(lock)
	if err := os.Symlink(host+"-999999999", lock); err != nil {
		t.Fatal(err)
	}
	if _, busy := ProfileInUse(dir); busy {
		t.Fatal("残留锁不应判为占用")
	}
}

func TestFindLocalChrome(t *testing.T) {
	p := setup.Host()
	p.GOOS = "linux"
	p.LookPath = func(string) (string, error) { return "", os.ErrNotExist }
	if got, err := FindLocalChrome(config.BrowserConfig{ChromePath: "/my/chrome"}, p); err != nil || got != "/my/chrome" {
		t.Fatal(got, err)
	}
	p.LookPath = func(name string) (string, error) { return "/usr/bin/" + name, nil }
	if got, err := FindLocalChrome(config.BrowserConfig{CDPLaunch: []string{"/mnt/c/chrome.exe"}}, p); err != nil || got != "/usr/bin/google-chrome" {
		t.Fatal("不应使用 cdp_launch[0]:", got, err)
	}
}

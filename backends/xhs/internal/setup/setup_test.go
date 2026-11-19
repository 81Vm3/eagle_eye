package setup

import (
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"eagle-eye/xhs/internal/config"
)

// fake 注入的平台：existing 里的路径存在，path 里的命令可在 PATH 找到。
func fake(goos string, existing []string, path map[string]string, env map[string]string) Platform {
	return Platform{
		GOOS:   goos,
		Exists: func(p string) bool { return slices.Contains(existing, p) },
		LookPath: func(name string) (string, error) {
			if p, ok := path[name]; ok {
				return p, nil
			}
			return "", errors.New("not found")
		},
		Getenv: func(k string) string { return env[k] },
	}
}

const (
	macChrome   = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
	macChromium = "/Applications/Chromium.app/Contents/MacOS/Chromium"
	macEdge     = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
)

func TestFindChrome(t *testing.T) {
	winEnv := map[string]string{"ProgramFiles": `C:\Program Files`, "ProgramFiles(x86)": `C:\Program Files (x86)`, "LOCALAPPDATA": `C:\Users\u\AppData\Local`}
	cases := []struct {
		name  string
		p     Platform
		want  string
		found bool
	}{
		{"mac chrome", fake("darwin", []string{macChrome, macEdge}, nil, nil), macChrome, true},
		{"mac 用户目录 chrome", fake("darwin", []string{"/Users/u/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}, nil, map[string]string{"HOME": "/Users/u"}),
			"/Users/u/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", true},
		{"mac 退到 chromium", fake("darwin", []string{macChromium, macEdge}, nil, nil), macChromium, true},
		{"mac 退到 edge", fake("darwin", []string{macEdge}, nil, nil), macEdge, true},
		{"mac 都没有", fake("darwin", nil, nil, nil), macChrome, false},
		{"linux PATH", fake("linux", nil, map[string]string{"chromium": "/usr/bin/chromium"}, nil), "/usr/bin/chromium", true},
		{"linux google-chrome 优先", fake("linux", nil, map[string]string{"chromium": "/usr/bin/chromium", "google-chrome": "/usr/bin/google-chrome"}, nil), "/usr/bin/google-chrome", true},
		{"linux 都没有", fake("linux", nil, nil, nil), "google-chrome", false},
		{"windows chrome", fake("windows", []string{`C:\Program Files\Google\Chrome\Application\chrome.exe`}, nil, winEnv),
			`C:\Program Files\Google\Chrome\Application\chrome.exe`, true},
		{"windows 用户级 chrome", fake("windows", []string{`C:\Users\u\AppData\Local\Google\Chrome\Application\chrome.exe`}, nil, winEnv),
			`C:\Users\u\AppData\Local\Google\Chrome\Application\chrome.exe`, true},
		{"windows 退到 edge", fake("windows", []string{`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`}, nil, nil),
			`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`, true},
	}
	for _, c := range cases {
		got, found := FindChrome(c.p)
		if got != c.want || found != c.found {
			t.Errorf("%s: got %q %v, want %q %v", c.name, got, found, c.want, c.found)
		}
	}
}

func TestLaunchFlagsPerPlatform(t *testing.T) {
	for _, goos := range []string{"darwin", "linux", "windows"} {
		f := LaunchFlags(goos)
		for _, want := range []string{"--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding", "--disable-background-timer-throttling"} {
			if !slices.Contains(f, want) {
				t.Errorf("%s 缺少 %s", goos, want)
			}
		}
		if win := slices.Contains(f, "--disable-features=CalculateNativeWinOcclusion"); win != (goos == "windows") {
			t.Errorf("%s: CalculateNativeWinOcclusion=%v", goos, win)
		}
	}
	l := DefaultLaunch("darwin", macChrome, "http://127.0.0.1:9333", "/x/data/chrome-profile")
	if l[0] != macChrome || l[1] != "--remote-debugging-port=9333" || l[2] != "--user-data-dir=/x/data/chrome-profile" {
		t.Fatal(l)
	}
	if Port("") != "9222" || Port("http://localhost") != "9222" {
		t.Fatal("默认端口应为 9222")
	}
}

// DefaultConfig 生成的内容必须能被 config.Load 读出预期的值。
func TestDefaultConfigLoads(t *testing.T) {
	winProfile := `C:\Users\it's me\AppData\Roaming\eagle_eye\xhs\data\chrome-profile`
	cases := []struct {
		p       Platform
		profile string
		chrome  string
		found   bool
		winFlag bool
	}{
		{fake("darwin", []string{macChrome}, nil, nil), "/Users/u/Library/Application Support/eagle_eye/xhs/data/chrome-profile", macChrome, true, false},
		{fake("linux", nil, map[string]string{"google-chrome": "/usr/bin/google-chrome"}, nil), "/home/u/.config/eagle_eye/xhs/data/chrome-profile", "/usr/bin/google-chrome", true, false},
		{fake("linux", nil, nil, nil), "/home/u/.config/eagle_eye/xhs/data/chrome-profile", "google-chrome", false, false},
		{fake("windows", []string{`C:\Program Files\Google\Chrome\Application\chrome.exe`}, nil, map[string]string{"ProgramFiles": `C:\Program Files`}),
			winProfile, `C:\Program Files\Google\Chrome\Application\chrome.exe`, true, true},
	}
	for _, c := range cases {
		content, found := DefaultConfig(c.p, c.profile)
		if found != c.found {
			t.Errorf("%s: found=%v", c.p.GOOS, found)
		}
		if strings.Contains(content, "/mnt/c") || strings.Contains(content, "<你的用户名>") {
			t.Errorf("%s: 不应包含 WSL/个人路径示例", c.p.GOOS)
		}
		if !found && !strings.Contains(content, "没有在本机找到") {
			t.Errorf("%s: 找不到浏览器时应有提示注释", c.p.GOOS)
		}
		path := filepath.Join(t.TempDir(), "config.yaml")
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
		cfg, err := config.Load(path)
		if err != nil {
			t.Fatalf("%s: %v\n%s", c.p.GOOS, err, content)
		}
		b := cfg.Browser
		// 新生成的配置显式写 headless: true（即使同时写了 cdp_url，也不落入"旧配置按 false"的兼容规则）
		if b.CDPURL != DefaultCDPURL || b.Silent || !b.Headless || b.HeadlessSet == nil || !*b.HeadlessSet || b.ChromePath != "" {
			t.Errorf("%s: browser %+v", c.p.GOOS, b)
		}
		if !strings.Contains(content, "\n  headless: true\n") {
			t.Errorf("%s: 应显式写 headless: true", c.p.GOOS)
		}
		want := DefaultLaunch(c.p.GOOS, c.chrome, DefaultCDPURL, c.profile)
		if !slices.Equal(b.CDPLaunch, want) {
			t.Errorf("%s: cdp_launch\n got %q\nwant %q", c.p.GOOS, b.CDPLaunch, want)
		}
		if slices.Contains(b.CDPLaunch, "--disable-features=CalculateNativeWinOcclusion") != c.winFlag {
			t.Errorf("%s: CalculateNativeWinOcclusion 只应在 Windows", c.p.GOOS)
		}
		if b.UserDataDir != filepath.Join(filepath.Dir(path), "data", "chrome-profile") ||
			cfg.DBPath != filepath.Join(filepath.Dir(path), "data", "xhs.db") {
			t.Errorf("%s: paths %q %q", c.p.GOOS, b.UserDataDir, cfg.DBPath)
		}
		if cfg.Behavior.CooldownHours != 6 || cfg.Channel.DetailConcurrency != 3 || cfg.Channel.TimeoutSeconds != 120 || cfg.Channel.MaxScrolls != 5 {
			t.Errorf("%s: %+v %+v", c.p.GOOS, cfg.Behavior, cfg.Channel)
		}
	}
}

func TestInitWritesOnceAndNeverOverwrites(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "home with space", "xhs")
	path := filepath.Join(dir, "config.yaml")
	p := fake("darwin", []string{macChrome}, nil, nil)

	res, err := Init(path, p)
	if err != nil || !res.Created || res.Chrome != macChrome || !res.ChromeFound {
		t.Fatalf("%+v %v", res, err)
	}
	cfg, err := config.Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if want := "--user-data-dir=" + filepath.Join(dir, "data", "chrome-profile"); cfg.Browser.CDPLaunch[2] != want {
		t.Fatalf("profile 应指向配置目录下 data/chrome-profile: %q", cfg.Browser.CDPLaunch[2])
	}

	if err := os.WriteFile(path, []byte("cdp_url: mine\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	res, err = Init(path, p)
	if err != nil || res.Created {
		t.Fatalf("已存在时应不覆盖且不报错: %+v %v", res, err)
	}
	if b, _ := os.ReadFile(path); string(b) != "cdp_url: mine\n" {
		t.Fatalf("内容被改动: %q", b)
	}
}

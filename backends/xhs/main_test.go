package main

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"eagle-eye/xhs/internal/setup"
	"eagle-eye/xhs/internal/xhs"
)

var xhsStart startFunc = xhs.StartChrome

func macPlatform(chromeExists bool) setup.Platform {
	p := setup.Host()
	p.GOOS = "darwin"
	p.Getenv = func(string) string { return "" }
	p.Exists = func(path string) bool {
		return chromeExists && path == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
	}
	return p
}

func TestVersionDefaultAndLdflags(t *testing.T) {
	if testing.Short() {
		t.Skip("需要编译")
	}
	bin := filepath.Join(t.TempDir(), "eagle-xhs")
	if out, err := exec.Command("go", "build", "-ldflags", "-X main.version=9.8.7", "-o", bin, ".").CombinedOutput(); err != nil {
		if _, lookErr := exec.LookPath("go"); lookErr != nil {
			t.Skip("PATH 中没有 go")
		}
		t.Fatalf("%v\n%s", err, out)
	}
	out, err := exec.Command(bin, "version").Output()
	if err != nil || string(out) != "9.8.7\n" {
		t.Fatalf("version: %q %v", out, err)
	}
	if version != "dev" {
		t.Fatalf("未注入时应为 dev: %q", version)
	}
}

func TestInitCmd(t *testing.T) {
	path := filepath.Join(t.TempDir(), "xhs", "config.yaml")
	var stderr bytes.Buffer
	if code := runInitCmd([]string{"-config", path}, &stderr, macPlatform(true)); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	b, err := os.ReadFile(path)
	if err != nil || !strings.Contains(string(b), "Google Chrome.app") || !strings.Contains(stderr.String(), "已生成") {
		t.Fatalf("%v\n%s\n%s", err, b, stderr.String())
	}
	stderr.Reset()
	if code := runInitCmd([]string{"-config", path}, &stderr, macPlatform(true)); code != 0 ||
		!strings.Contains(stderr.String(), "已存在") {
		t.Fatalf("已存在应 exit 0 并提示: %d %s", code, stderr.String())
	}
	if b2, _ := os.ReadFile(path); !bytes.Equal(b, b2) {
		t.Fatal("已存在的配置被改动")
	}
	// 找不到浏览器也写入（带警告），exit 0
	stderr.Reset()
	path2 := filepath.Join(t.TempDir(), "config.yaml")
	if code := runInitCmd([]string{"-config", path2}, &stderr, macPlatform(false)); code != 0 ||
		!strings.Contains(stderr.String(), "警告") {
		t.Fatalf("%d %s", code, stderr.String())
	}
}

func writeConfig(t *testing.T, yaml string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(yaml), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func noStart(t *testing.T) startFunc {
	return func(context.Context, []string, string, time.Duration) error {
		t.Fatal("不应启动 Chrome")
		return nil
	}
}

func TestBrowserAlreadyRunning(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, `{"webSocketDebuggerUrl":"ws://%s/devtools/browser/x"}`, r.Host)
	}))
	defer srv.Close()
	path := writeConfig(t, "browser:\n  cdp_url: "+srv.URL+"\n  cdp_launch: ['/nonexistent/chrome-for-test']\n")
	var stderr bytes.Buffer
	if code := runBrowserCmd([]string{"-config", path}, &stderr, macPlatform(false), noStart(t)); code != 0 ||
		!strings.Contains(stderr.String(), "已在运行") {
		t.Fatalf("%d %s", code, stderr.String())
	}
}

func TestBrowserLaunch(t *testing.T) {
	url := "http://127.0.0.1:1" // 端口 1：没有程序监听
	// 配置了 cdp_launch：原样使用
	path := writeConfig(t, "browser:\n  cdp_url: "+url+"\n  cdp_launch: ['/nonexistent/chrome-for-test', '--x']\n")
	var got []string
	ok := func(_ context.Context, launch []string, u string, _ time.Duration) error {
		got = launch
		if u != url {
			t.Errorf("cdp_url %q", u)
		}
		return nil
	}
	var stderr bytes.Buffer
	if code := runBrowserCmd([]string{"-config", path}, &stderr, macPlatform(false), ok); code != 0 ||
		!slices.Equal(got, []string{"/nonexistent/chrome-for-test", "--x"}) || !strings.Contains(stderr.String(), "已就绪") {
		t.Fatalf("%d %q %s", code, got, stderr.String())
	}

	// 没配 cdp_launch：用平台默认，profile = 配置目录下 data/chrome-profile
	path = writeConfig(t, "browser:\n  cdp_url: "+url+"\n")
	got = nil
	if code := runBrowserCmd([]string{"-config", path}, &stderr, macPlatform(true), ok); code != 0 {
		t.Fatal(code)
	}
	want := setup.DefaultLaunch("darwin", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", url,
		filepath.Join(filepath.Dir(path), "data", "chrome-profile"))
	if !slices.Equal(got, want) {
		t.Fatalf("got %q\nwant %q", got, want)
	}

	// 没配 cdp_launch 且找不到浏览器：exit 1，不启动
	stderr.Reset()
	if code := runBrowserCmd([]string{"-config", path}, &stderr, macPlatform(false), noStart(t)); code != 1 ||
		!strings.Contains(stderr.String(), "没有在本机找到") {
		t.Fatalf("%d %s", code, stderr.String())
	}

	// 真实启动路径：不存在的 Chrome → exit 1
	stderr.Reset()
	path = writeConfig(t, "browser:\n  cdp_url: "+url+"\n  cdp_launch: ['/nonexistent/chrome-for-test']\n")
	if code := runBrowserCmd([]string{"-config", path, "-timeout", "1s"}, &stderr, macPlatform(false), xhsStart); code != 1 ||
		!strings.Contains(stderr.String(), "启动失败") {
		t.Fatalf("%d %s", code, stderr.String())
	}

	// 配置文件不存在：提示 init
	stderr.Reset()
	if code := runBrowserCmd([]string{"-config", filepath.Join(t.TempDir(), "none.yaml")}, &stderr, macPlatform(true), noStart(t)); code != 1 ||
		!strings.Contains(stderr.String(), "eagle-xhs init") {
		t.Fatalf("%d %s", code, stderr.String())
	}
}

// headless=true 时 browser 子命令照常可用（调试），但提示默认不再需要它
func TestBrowserHeadlessHint(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, `{"webSocketDebuggerUrl":"ws://%s/devtools/browser/x"}`, r.Host)
	}))
	defer srv.Close()
	path := writeConfig(t, "browser:\n  headless: true\n  cdp_url: "+srv.URL+"\n")
	var stderr bytes.Buffer
	if code := runBrowserCmd([]string{"-config", path}, &stderr, macPlatform(false), noStart(t)); code != 0 ||
		!strings.Contains(stderr.String(), "不需要 eagle xhs browser") {
		t.Fatalf("%d %s", code, stderr.String())
	}
}

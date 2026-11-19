package xhs

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"runtime"
	"strings"
	"testing"
	"time"
)

// fakeCDP 模拟 Chrome 调试端口的 /json/version。
func fakeCDP(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/json/version" {
			http.NotFound(w, r)
			return
		}
		fmt.Fprintf(w, `{"Browser":"Chrome/1","webSocketDebuggerUrl":"ws://%s/devtools/browser/x"}`, r.Host)
	}))
	t.Cleanup(srv.Close)
	return srv
}

// closedPort 一个当前没有程序监听的本地地址。
func closedPort(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	l.Close()
	return addr
}

func TestCDPReady(t *testing.T) {
	ctx := context.Background()
	srv := fakeCDP(t)
	port := srv.Listener.Addr().(*net.TCPAddr).Port
	for _, u := range []string{srv.URL, strings.Replace(srv.URL, "http", "ws", 1), fmt.Sprint(port), fmt.Sprintf("127.0.0.1:%d", port)} {
		if !CDPReady(ctx, u) {
			t.Errorf("%s 应可连", u)
		}
	}
	if CDPReady(ctx, "http://"+closedPort(t)) || CDPReady(ctx, "") {
		t.Error("没有监听的端口不应可连")
	}
	// 端口上是别的 HTTP 服务（没有 webSocketDebuggerUrl）
	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, `{}`) }))
	defer other.Close()
	if CDPReady(ctx, other.URL) {
		t.Error("非 Chrome 的服务不应算可连")
	}
	// 端口上的程序不应答：必须超时返回而不是挂住
	hang := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { <-r.Context().Done() }))
	defer hang.Close()
	start := time.Now()
	if CDPReady(ctx, hang.URL) || time.Since(start) > 5*time.Second {
		t.Error("不应答的端口应在超时后返回 false")
	}
}

func TestStartChromeErrors(t *testing.T) {
	ctx := context.Background()
	if err := StartChrome(ctx, nil, "9222", time.Second); err != ErrNoLaunch {
		t.Fatal(err)
	}
	url := "http://" + closedPort(t)
	if err := StartChrome(ctx, []string{"/nonexistent/chrome-for-test"}, url, time.Second); err == nil ||
		!strings.Contains(err.Error(), "启动 Chrome") {
		t.Fatalf("不存在的可执行文件应立即报错: %v", err)
	}
	if runtime.GOOS == "windows" {
		return
	}
	// 进程立即退出、端口始终不可用：不等满超时，直接报错
	start := time.Now()
	err := StartChrome(ctx, []string{"/bin/sh", "-c", "exit 0"}, url, 20*time.Second)
	if err == nil || !strings.Contains(err.Error(), "已退出") || time.Since(start) > 5*time.Second {
		t.Fatalf("进程退出应尽快报错: %v (%s)", err, time.Since(start))
	}
	// 进程一直在但端口不就绪：按超时报错
	err = StartChrome(ctx, []string{"/bin/sh", "-c", "sleep 5"}, url, time.Second)
	if err == nil || !strings.Contains(err.Error(), "未就绪") {
		t.Fatalf("应超时: %v", err)
	}
}

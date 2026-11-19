package xhs

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os/exec"
	"strings"
	"time"
)

// ErrNoLaunch 没有可用的启动命令。
var ErrNoLaunch = errors.New("没有配置 cdp_launch")

// CDPReady 调试端口上是否有 Chrome 在应答：GET /json/version 返回 webSocketDebuggerUrl。
// 自带超时（rod 的 launcher.ResolveURL 没有超时，端口被别的程序占着不应答时会一直挂住）。
func CDPReady(ctx context.Context, cdpURL string) bool {
	u, err := url.Parse(normalizeCDP(cdpURL))
	if err != nil || u.Host == "" {
		return false
	}
	switch u.Scheme {
	case "ws":
		u.Scheme = "http"
	case "wss":
		u.Scheme = "https"
	}
	u.Path, u.RawQuery = "/json/version", ""
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return false
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		return false
	}
	defer res.Body.Close()
	var v struct {
		WS string `json:"webSocketDebuggerUrl"`
	}
	return res.StatusCode == http.StatusOK && json.NewDecoder(res.Body).Decode(&v) == nil && v.WS != ""
}

// normalizeCDP 与 rod 的 launcher.ResolveURL 一样接受 "9222"、"127.0.0.1:9222"、"http://…"、"ws://…"。
func normalizeCDP(s string) string {
	s = strings.TrimSpace(s)
	if s != "" && strings.Trim(s, "0123456789") == "" {
		s = "127.0.0.1:" + s
	}
	if !strings.Contains(s, "://") {
		s = "http://" + s
	}
	return s
}

// StartChrome 执行 launch 启动专用 Chrome 并等待 cdpURL 就绪（最多 wait）。
// Chrome 脱离本进程（独立会话、stdin/stdout/stderr 指向空设备）：本程序退出、终端关闭、
// 调用方等待本进程输出管道时都不受 Chrome 影响，Chrome 一直运行到用户关闭它。
func StartChrome(ctx context.Context, launch []string, cdpURL string, wait time.Duration) error {
	if len(launch) == 0 || launch[0] == "" {
		return ErrNoLaunch
	}
	cmd := exec.Command(launch[0], launch[1:]...)
	detach(cmd)
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("启动 Chrome（%s）: %w", launch[0], err)
	}
	exited := make(chan error, 1)
	go func() { exited <- cmd.Wait() }()

	deadline := time.Now().Add(wait)
	for {
		if CDPReady(ctx, cdpURL) {
			return nil
		}
		select {
		case err := <-exited:
			// 同一 user-data-dir 的 Chrome 已在运行（没开调试端口）时，新进程会把请求交给它然后立即退出
			if CDPReady(ctx, cdpURL) {
				return nil
			}
			return fmt.Errorf("Chrome 进程已退出（%v），调试端口仍不可用。如果这个 --user-data-dir 的 Chrome "+
				"已经在运行且没开调试端口，请先完全关闭它再重试", err)
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(500 * time.Millisecond):
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("Chrome 已启动，但调试端口 %s 在 %s 内未就绪"+
				"（如果这个 --user-data-dir 的 Chrome 已经在运行且没开调试端口，请先完全关闭它）", cdpURL, wait)
		}
	}
}

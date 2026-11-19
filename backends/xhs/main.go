// eagle-xhs：eagle_eye 的小红书搜索渠道后端。
//
//	eagle-xhs channel [-config path] [-v] describe|search   eagle_eye 渠道协议（JSON-over-stdio）
//	eagle-xhs login   [-config path]                        打开浏览器，手动扫码登录（只需一次）
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"eagle-eye/xhs/internal/channel"
	"eagle-eye/xhs/internal/config"
	"eagle-eye/xhs/internal/lock"
	"eagle-eye/xhs/internal/store"
	"eagle-eye/xhs/internal/xhs"
)

func usage() {
	fmt.Fprint(os.Stderr, `用法: eagle-xhs <channel|login> [选项]

  channel [-config path] [-v] describe|search
          eagle_eye 渠道协议：describe 输出渠道元数据；search 从 stdin 读 Query JSON，
          stdout 只输出一个 JSON 对象，日志写 stderr
  login   [-config path] [-v]
          打开浏览器，在窗口中手动扫码登录小红书（登录态保存在浏览器 profile 里）

选项:
  -config string     配置文件 (默认 config.yaml)
  -v                 输出调试日志
`)
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "channel":
		os.Exit(runChannel(os.Args[2:]))
	case "login":
		os.Exit(runLoginCmd(os.Args[2:]))
	default:
		usage()
		os.Exit(2)
	}
}

// runLoginCmd login 子命令入口，返回退出码。
func runLoginCmd(args []string) int {
	fs := flag.NewFlagSet("login", flag.ExitOnError)
	cfgPath := fs.String("config", "config.yaml", "配置文件路径")
	verbose := fs.Bool("v", false, "调试日志")
	_ = fs.Parse(args)

	level := slog.LevelInfo
	if *verbose {
		level = slog.LevelDebug
	}
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: level}))

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		log.Error("加载配置失败", "err", err)
		return 1
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// login 驱动浏览器：与 channel search 互斥，同一时刻只允许一个进程使用浏览器
	lk, err := lock.TryLock(cfg.LockPath(), "login")
	if err != nil {
		log.Error("无法获取浏览器锁，退出", "err", err, "lock", cfg.LockPath())
		return 1
	}
	defer lk.Unlock()

	if err := runLogin(ctx, cfg, log); err != nil && !errors.Is(err, context.Canceled) {
		log.Error("退出", "err", err)
		return 1
	}
	return 0
}

func runLogin(ctx context.Context, cfg *config.Config, log *slog.Logger) error {
	cfg.Browser.Headless = false
	sess, err := xhs.OpenBrowser(cfg.Browser)
	if err != nil {
		return err
	}
	defer sess.Close()
	c := xhs.NewClient(sess.Page, xhs.NewHuman())
	log.Info("请在弹出的浏览器窗口中扫码登录小红书（最多等待 5 分钟）…")
	if err := c.WaitForLogin(ctx, 5*time.Minute); err != nil {
		return err
	}
	log.Info("登录成功，登录态已保存到浏览器配置目录", "dir", cfg.Browser.UserDataDir)
	return nil
}

// runChannel eagle_eye 渠道协议入口，返回退出码。stdout 只输出一个 JSON 对象。
func runChannel(args []string) (code int) {
	out := os.Stdout
	os.Stdout = os.Stderr // 任何库误写 stdout 都会破坏协议输出：全部改道到 stderr
	written := false
	emit := func(v any) {
		if !written {
			written = true
			_ = channel.WriteJSON(out, v)
		}
	}
	fail := func(err error) int {
		if !written {
			written = true
			_ = channel.WriteError(out, err)
		}
		return 1
	}
	defer func() {
		if p := recover(); p != nil {
			fmt.Fprintf(os.Stderr, "panic: %v\n", p)
			code = fail(channel.Errorf(channel.CodeInternal, "panic: %v", p))
		}
	}()

	fs := flag.NewFlagSet("channel", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	cfgPath := fs.String("config", "config.yaml", "配置文件路径")
	verbose := fs.Bool("v", false, "调试日志")
	if err := fs.Parse(args); err != nil {
		return fail(channel.Errorf(channel.CodeInternal, "参数错误: %v", err))
	}
	level := slog.LevelInfo
	if *verbose {
		level = slog.LevelDebug
	}
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: level})).With("channel", channel.Name)

	switch verb := fs.Arg(0); verb {
	case "describe":
		emit(channel.Describe())
		return 0
	case "search":
	default:
		usage()
		return fail(channel.Errorf(channel.CodeInternal, "未知的 channel 动词 %q（应为 describe 或 search）", verb))
	}

	q, err := channel.ParseQuery(os.Stdin)
	if err != nil {
		return fail(err)
	}
	cfg, err := config.Load(*cfgPath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return fail(channel.Errorf(channel.CodeUnavailable, "配置文件不存在: %v", err))
		}
		return fail(channel.Errorf(channel.CodeInternal, "加载配置: %v", err))
	}
	db, err := store.Open(cfg.DBPath)
	if err != nil {
		return fail(channel.Errorf(channel.CodeInternal, "打开数据库: %v", err))
	}
	defer db.Close()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	resp, err := channel.NewRunner(cfg, db, log).Search(ctx, q)
	if err != nil {
		e := channel.AsError(err)
		log.Warn("搜索失败", "code", e.Code, "err", e.Message)
		return fail(e)
	}
	emit(resp)
	return 0
}

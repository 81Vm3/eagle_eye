// eagle-xhs：eagle_eye 的小红书搜索渠道后端。
//
//	eagle-xhs channel [-config path] [-v] describe|search   eagle_eye 渠道协议（JSON-over-stdio）
//	eagle-xhs login   [-config path]                        打开有界面的 Chrome，手动扫码登录（只需一次），完成后关闭
//	eagle-xhs browser [-config path]                        启动带调试端口的专用 Chrome（headless=false / 调试用）
//	eagle-xhs init    [-config path]                        写入适合当前平台的默认配置（已存在不覆盖）
//	eagle-xhs version                                       输出版本
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"slices"
	"syscall"
	"time"

	"eagle-eye/xhs/internal/channel"
	"eagle-eye/xhs/internal/config"
	"eagle-eye/xhs/internal/lock"
	"eagle-eye/xhs/internal/setup"
	"eagle-eye/xhs/internal/store"
	"eagle-eye/xhs/internal/xhs"
)

// version 由构建注入：go build -ldflags "-X main.version=1.2.3"
var version = "dev"

func usage() {
	fmt.Fprint(os.Stderr, `用法: eagle-xhs <channel|login|browser|init|version> [选项]

  channel [-config path] [-v] describe|search
          eagle_eye 渠道协议：describe 输出渠道元数据；search 从 stdin 读 Query JSON，
          stdout 只输出一个 JSON 对象，日志写 stderr
  login   [-config path] [-v]
          打开有界面的 Chrome，在窗口中手动扫码登录小红书（登录态保存在专用 profile 里），登录后关闭窗口
  browser [-config path] [-timeout 30s]
          按 browser.cdp_launch（未配置时用本平台默认）启动带远程调试端口的专用 Chrome，
          端口就绪后退出（Chrome 继续运行）；端口已可连时直接返回。
          默认的 browser.headless=true 下 channel search 自己启动无头 Chrome，不需要它
  init    [-config path]
          在 path 写入适合当前平台的默认配置；文件已存在时不覆盖
  version 输出版本

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
	case "browser":
		os.Exit(runBrowserCmd(os.Args[2:], os.Stderr, setup.Host(), xhs.StartChrome))
	case "init":
		os.Exit(runInitCmd(os.Args[2:], os.Stderr, setup.Host()))
	case "version", "-version", "--version":
		fmt.Fprintln(os.Stdout, version)
	default:
		usage()
		os.Exit(2)
	}
}

// runInitCmd init 子命令：写入默认配置，已存在时不覆盖（仍 exit 0）。
func runInitCmd(args []string, stderr io.Writer, p setup.Platform) int {
	fs := flag.NewFlagSet("init", flag.ContinueOnError)
	fs.SetOutput(stderr)
	cfgPath := fs.String("config", "config.yaml", "要生成的配置文件路径")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	res, err := setup.Init(*cfgPath, p)
	if err != nil {
		fmt.Fprintf(stderr, "生成配置失败: %v\n", err)
		return 1
	}
	if !res.Created {
		fmt.Fprintf(stderr, "配置文件已存在，未覆盖: %s\n", *cfgPath)
		return 0
	}
	fmt.Fprintf(stderr, "已生成配置: %s\n", *cfgPath)
	if res.ChromeFound {
		fmt.Fprintf(stderr, "专用 Chrome 使用: %s\n", res.Chrome)
	} else {
		fmt.Fprintf(stderr, "警告：没有在本机找到 Chrome / Chromium / Edge，已写入默认路径 %s；"+
			"请安装 Chrome，或在配置里设置 browser.chrome_path（无头搜索与 login 用）/ browser.cdp_launch（headless: false 时用）\n", res.Chrome)
	}
	return 0
}

// startFunc 启动 Chrome 并等待调试端口就绪（测试可替换）。
type startFunc func(ctx context.Context, launch []string, cdpURL string, wait time.Duration) error

// runBrowserCmd browser 子命令：启动带调试端口的专用 Chrome，端口就绪后退出，Chrome 继续运行。
// 不驱动浏览器、不打开任何网页，因此不拿浏览器锁。
func runBrowserCmd(args []string, stderr io.Writer, p setup.Platform, start startFunc) int {
	fs := flag.NewFlagSet("browser", flag.ContinueOnError)
	fs.SetOutput(stderr)
	cfgPath := fs.String("config", "config.yaml", "配置文件路径")
	wait := fs.Duration("timeout", 30*time.Second, "等待调试端口就绪的最长时间")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	cfg, err := config.Load(*cfgPath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			fmt.Fprintf(stderr, "配置文件不存在: %s（先运行 eagle-xhs init -config %s 生成）\n", *cfgPath, *cfgPath)
		} else {
			fmt.Fprintf(stderr, "加载配置失败: %v\n", err)
		}
		return 1
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if cfg.Browser.Headless {
		fmt.Fprintln(stderr, "提示：当前 browser.headless=true，channel search 会自己用专用 profile 启动无头 Chrome，不需要 eagle xhs browser。"+
			"这个窗口开着期间会占用专用 profile，channel search / login 会报 profile 被占用；调试完请关闭它")
	}

	cdpURL := cfg.Browser.CDPURL
	if cdpURL == "" {
		cdpURL = setup.DefaultCDPURL
		fmt.Fprintf(stderr, "提示：配置里没有 browser.cdp_url，按 %s 启动；channel search 需要在配置里设置它\n", cdpURL)
	}
	if xhs.CDPReady(ctx, cdpURL) {
		fmt.Fprintf(stderr, "专用 Chrome 已在运行（%s），无需启动\n", cdpURL)
		return 0
	}
	launch := cfg.Browser.CDPLaunch
	if len(launch) == 0 {
		chrome, found := setup.FindChrome(p)
		if !found {
			fmt.Fprintf(stderr, "没有在本机找到 Chrome / Chromium / Edge（查找过: %v）；"+
				"请安装 Chrome，或在配置里设置 browser.cdp_launch\n", setup.ChromeCandidates(p))
			return 1
		}
		launch = setup.DefaultLaunch(p.GOOS, chrome, cdpURL, cfg.Browser.UserDataDir)
	}
	// 专用 profile 正被无头搜索或 login 使用时，新启动的 Chrome 会把请求交给那个进程后立即退出，调试端口永远等不到
	if slices.Contains(launch, "--user-data-dir="+cfg.Browser.UserDataDir) {
		if pid, busy := xhs.ProfileInUse(cfg.Browser.UserDataDir); busy {
			fmt.Fprintf(stderr, "专用 profile %s 正被另一个 Chrome（pid %d）使用（可能是正在进行的 channel search 或 login），请稍后再试\n",
				cfg.Browser.UserDataDir, pid)
			return 1
		}
	}
	fmt.Fprintf(stderr, "启动专用 Chrome: %s\n", launch[0])
	if err := start(ctx, launch, cdpURL, *wait); err != nil {
		fmt.Fprintf(stderr, "启动失败: %v\n", err)
		return 1
	}
	fmt.Fprintf(stderr, "专用 Chrome 已就绪（%s）。首次使用请运行 eagle xhs login 扫码登录小红书\n", cdpURL)
	return 0
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
	// 扫码必须有界面：无论 browser.headless 怎么配置，login 都打开有界面的 Chrome（见 xhs.LoginConfig）
	sess, err := xhs.OpenLogin(cfg.Browser)
	if err != nil {
		return err
	}
	defer sess.Close()
	c := xhs.NewClient(sess.Page)
	log.Info("请在弹出的浏览器窗口中扫码登录小红书（最多等待 5 分钟）…")
	onVerify := func() {
		log.Warn("小红书弹出了安全验证（风控），请在浏览器窗口里完成验证；完成后继续扫码登录，程序会一直等待")
	}
	if err := c.WaitForLogin(ctx, 5*time.Minute, onVerify); err != nil {
		return err
	}
	if cfg.Browser.Headless {
		log.Info("登录成功，登录态已保存到专用 profile，关闭窗口；之后 channel search 用同一个 profile 无头运行", "dir", cfg.Browser.UserDataDir)
	} else {
		log.Info("登录成功，登录态已保存到浏览器配置目录", "dir", cfg.Browser.UserDataDir)
	}
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

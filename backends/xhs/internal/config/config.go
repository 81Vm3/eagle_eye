// Package config 读取 YAML 配置并补全默认值。
package config

import (
	"fmt"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

// Range 闭区间 [Min, Max]，YAML 中写作 [a, b]。
type Range [2]int

func (r Range) Min() int { return r[0] }
func (r Range) Max() int { return r[1] }

type Config struct {
	DBPath   string         `yaml:"db_path"`
	Browser  BrowserConfig  `yaml:"browser"`
	Behavior BehaviorConfig `yaml:"behavior"`
	// Channel 作为 eagle_eye 渠道（channel search）时的参数
	Channel ChannelConfig `yaml:"channel"`
}

type BrowserConfig struct {
	// CDPURL 非空时接管一个已经在运行的 Chrome（例如 http://127.0.0.1:9222），
	// 这是最不容易被识别的方式：就是你自己日常用的浏览器。
	CDPURL string `yaml:"cdp_url"`
	// CDPLaunch cdp_url 连不上时用来启动 Chrome 的命令（可执行文件 + 参数），为空则不自动启动。
	CDPLaunch   []string `yaml:"cdp_launch"`
	ChromePath  string   `yaml:"chrome_path"`
	UserDataDir string   `yaml:"user_data_dir"`
	Headless    bool     `yaml:"headless"`
	// Silent 静默运行：把有界面的 Chrome 窗口移到屏幕外（不是无头、不是最小化），退出时放回原处
	Silent bool   `yaml:"silent"`
	Proxy  string `yaml:"proxy"`
}

// BehaviorConfig 拟人节奏与风控冷却。
type BehaviorConfig struct {
	// ReadSeconds 每篇打开的笔记的阅读停留（秒）
	ReadSeconds Range `yaml:"read_seconds"`
	// ActionDelayMs 相邻操作之间的停顿（毫秒）
	ActionDelayMs Range `yaml:"action_delay_ms"`
	// CooldownHours 触发验证页后的冷却时长（连续触发翻倍，上限 48h）
	CooldownHours float64 `yaml:"cooldown_hours"`
	// NavIntervalSeconds 相邻两次页面加载的最小间隔（随机取值范围，秒），跨进程生效
	NavIntervalSeconds Range `yaml:"nav_interval_seconds"`
	// NavPerMinute 每分钟最多加载几个页面，-1 = 不限
	NavPerMinute int `yaml:"nav_per_minute"`
}

// ChannelConfig eagle_eye 渠道搜索（eagle-xhs channel search）的参数。
type ChannelConfig struct {
	// TimeoutSeconds 单次 channel search 的总时限（秒），应小于核心侧 timeout_seconds
	TimeoutSeconds int `yaml:"timeout_seconds"`
	// MaxScrolls 搜索结果页最多向下滚动几次
	MaxScrolls int `yaml:"max_scrolls"`
}

// Load 读取配置文件并补默认值。旧配置里已不再使用的段落会被忽略。
func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("读取配置 %s: %w", path, err)
	}
	cfg := &Config{}
	if err := yaml.Unmarshal(data, cfg); err != nil {
		return nil, fmt.Errorf("解析配置: %w", err)
	}
	cfg.applyDefaults()
	cfg.resolvePaths(filepath.Dir(path))
	return cfg, nil
}

// resolvePaths 把数据类相对路径解析为相对配置文件所在目录（而不是当前工作目录）：
// 无论从哪个目录、以什么 cwd 启动（手动 login、eagle_eye 核心拉起的 channel search），
// 只要用同一份配置，就一定落到同一个数据库、同一把浏览器锁上。
func (c *Config) resolvePaths(dir string) {
	abs := func(p *string) {
		if *p != "" && !filepath.IsAbs(*p) {
			*p = filepath.Join(dir, *p)
		}
	}
	abs(&c.DBPath)
	abs(&c.Browser.UserDataDir)
}

// LockPath 浏览器互斥锁文件：与数据库同目录的 browser.lock。
func (c *Config) LockPath() string {
	return filepath.Join(filepath.Dir(c.DBPath), "browser.lock")
}

func setRange(r *Range, a, b int) {
	if r[0] == 0 && r[1] == 0 {
		*r = Range{a, b}
	}
	if r[1] < r[0] {
		r[1] = r[0]
	}
}

func setInt(v *int, d int) {
	if *v == 0 {
		*v = d
	}
}

func setFloat(v *float64, d float64) {
	if *v == 0 {
		*v = d
	}
}

func (c *Config) applyDefaults() {
	if c.DBPath == "" {
		c.DBPath = "data/xhs.db"
	}
	if c.Browser.UserDataDir == "" {
		c.Browser.UserDataDir = "data/chrome-profile"
	}

	b := &c.Behavior
	setRange(&b.ReadSeconds, 5, 15)
	setRange(&b.ActionDelayMs, 600, 2200)
	setFloat(&b.CooldownHours, 6)
	setRange(&b.NavIntervalSeconds, 8, 15)
	setInt(&b.NavPerMinute, 3)

	ch := &c.Channel
	setInt(&ch.TimeoutSeconds, 600)
	setInt(&ch.MaxScrolls, 5)
	ch.MaxScrolls = max(0, min(ch.MaxScrolls, 10))
}

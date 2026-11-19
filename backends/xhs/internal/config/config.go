// Package config 读取 YAML 配置并补全默认值。
package config

import (
	"fmt"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

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
	// HeadlessSet 配置文件里写的 headless（nil = 没写）。实际生效的值见 Headless，由 Load 按兼容规则决定。
	HeadlessSet *bool `yaml:"headless"`
	// Headless 实际生效的模式（ResolveHeadless）：true 时 channel search 自己用 user_data_dir 启动
	// --headless=new 的 Chrome，用完关闭，不需要 cdp_url；false 时接管 cdp_url 的 Chrome。login 总是有界面。
	Headless bool `yaml:"-"`
	// Silent 静默运行：把有界面的 Chrome 窗口移到屏幕外（不是无头、不是最小化），退出时放回原处
	Silent bool   `yaml:"silent"`
	Proxy  string `yaml:"proxy"`
}

// BehaviorConfig 风控冷却。
//
// 旧版本的拟人节奏字段（read_seconds、action_delay_ms、nav_interval_seconds、nav_per_minute）已删除：
// channel search 不再模拟真人节奏。旧配置里残留这些字段时静默忽略（yaml 非严格解析）。
type BehaviorConfig struct {
	// CooldownHours 触发验证页后的冷却时长（连续触发翻倍，上限 48h）
	CooldownHours float64 `yaml:"cooldown_hours"`
}

// ChannelConfig eagle_eye 渠道搜索（eagle-xhs channel search）的参数。
type ChannelConfig struct {
	// TimeoutSeconds 单次 channel search 的总时限（秒），应小于核心侧 timeout_seconds
	TimeoutSeconds int `yaml:"timeout_seconds"`
	// MaxScrolls 搜索结果页最多向下滚动几次
	MaxScrolls int `yaml:"max_scrolls"`
	// DetailConcurrency detail 时最多同时打开几个笔记标签页（1–10，默认 3）
	DetailConcurrency int `yaml:"detail_concurrency"`
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

// ResolveHeadless headless 的兼容规则：
//   - 配置里显式写了 headless：以它为准；
//   - 没写、但配置了 cdp_url：旧配置（接管已运行 Chrome 的用法），按 false 处理，行为不变；
//   - 都没写：默认 true（新默认，channel search 不弹窗口）。
func ResolveHeadless(set *bool, cdpURL string) bool {
	if set != nil {
		return *set
	}
	return cdpURL == ""
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
	c.Browser.Headless = ResolveHeadless(c.Browser.HeadlessSet, c.Browser.CDPURL)

	setFloat(&c.Behavior.CooldownHours, 6)

	ch := &c.Channel
	setInt(&ch.TimeoutSeconds, 120)
	setInt(&ch.MaxScrolls, 5)
	ch.MaxScrolls = max(0, min(ch.MaxScrolls, 10))
	setInt(&ch.DetailConcurrency, 3)
	ch.DetailConcurrency = max(1, min(ch.DetailConcurrency, 10))
}

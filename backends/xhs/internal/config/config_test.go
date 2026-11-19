package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestPathsRelativeToConfigAndChannelDefaults(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "c.yaml")
	yaml := "db_path: data/xhs.db\nbrowser:\n  user_data_dir: /abs/profile\nchannel:\n  timeout_seconds: 300\n"
	if err := os.WriteFile(path, []byte(yaml), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.DBPath != filepath.Join(dir, "data", "xhs.db") || cfg.Browser.UserDataDir != "/abs/profile" {
		t.Fatalf("paths: %q %q", cfg.DBPath, cfg.Browser.UserDataDir)
	}
	if cfg.LockPath() != filepath.Join(dir, "data", "browser.lock") {
		t.Fatal(cfg.LockPath())
	}
	ch := cfg.Channel
	if ch.TimeoutSeconds != 300 || ch.MaxScrolls != 5 {
		t.Fatalf("channel defaults: %+v", ch)
	}
}

func TestDefaultsAndUnknownSections(t *testing.T) {
	path := filepath.Join(t.TempDir(), "c.yaml")
	// 旧配置里遗留的、已不再使用的段落应被忽略，不影响加载
	yaml := "legacy_section:\n  - name: x\nbehavior:\n  cooldown_hours: 2\nlimits:\n  removed_key: 5\n"
	if err := os.WriteFile(path, []byte(yaml), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Behavior.CooldownHours != 2 {
		t.Fatalf("behavior: %+v", cfg.Behavior)
	}
	if ch := cfg.Channel; ch.TimeoutSeconds != 120 || ch.MaxScrolls != 5 || ch.DetailConcurrency != 3 {
		t.Fatalf("channel defaults: %+v", ch)
	}
}

// 旧版本的拟人节奏字段已删除：旧配置里残留它们时静默忽略，不报错。
func TestRemovedPacingFieldsIgnored(t *testing.T) {
	path := filepath.Join(t.TempDir(), "c.yaml")
	yaml := `behavior:
  read_seconds: [5, 15]
  action_delay_ms: [600, 2200]
  cooldown_hours: 6
  nav_interval_seconds: [8, 15]
  nav_per_minute: 3
channel:
  timeout_seconds: 600
  max_scrolls: 5
`
	if err := os.WriteFile(path, []byte(yaml), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("旧配置应能加载: %v", err)
	}
	if cfg.Behavior.CooldownHours != 6 || cfg.Channel.DetailConcurrency != 3 {
		t.Fatalf("%+v %+v", cfg.Behavior, cfg.Channel)
	}
}

func TestDetailConcurrencyClamp(t *testing.T) {
	for in, want := range map[string]int{"": 3, "1": 1, "5": 5, "50": 10, "-2": 1} {
		path := filepath.Join(t.TempDir(), "c.yaml")
		body := "channel:\n"
		if in != "" {
			body += "  detail_concurrency: " + in + "\n"
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		cfg, err := Load(path)
		if err != nil {
			t.Fatal(err)
		}
		if got := cfg.Channel.DetailConcurrency; got != want {
			t.Errorf("detail_concurrency=%q → %d want %d", in, got, want)
		}
	}
}

func TestHeadlessCompat(t *testing.T) {
	cases := []struct {
		name, browser string
		want          bool
	}{
		{"新默认：都没写", "", true},
		{"旧配置：只有 cdp_url，未写 headless → false（保持接管 Chrome）", "  cdp_url: http://127.0.0.1:9222\n", false},
		{"旧配置：cdp_url 为空串", "  cdp_url: \"\"\n", true},
		{"显式 true 优先于 cdp_url", "  cdp_url: http://127.0.0.1:9222\n  headless: true\n", true},
		{"显式 false（旧 init 生成的配置）", "  headless: false\n", false},
		{"显式 false + cdp_url", "  cdp_url: http://127.0.0.1:9222\n  headless: false\n", false},
	}
	for _, c := range cases {
		path := filepath.Join(t.TempDir(), "c.yaml")
		if err := os.WriteFile(path, []byte("browser:\n"+c.browser), 0o644); err != nil {
			t.Fatal(err)
		}
		cfg, err := Load(path)
		if err != nil {
			t.Fatal(err)
		}
		if cfg.Browser.Headless != c.want {
			t.Errorf("%s: headless=%v want %v", c.name, cfg.Browser.Headless, c.want)
		}
	}
	tr := true
	if !ResolveHeadless(&tr, "x") || ResolveHeadless(nil, "x") || !ResolveHeadless(nil, "") {
		t.Fatal("ResolveHeadless")
	}
}

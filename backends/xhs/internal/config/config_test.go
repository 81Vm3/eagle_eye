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
	yaml := "legacy_section:\n  - name: x\nbehavior:\n  read_seconds: [9, 3]\n  nav_per_minute: -1\nlimits:\n  removed_key: 5\n"
	if err := os.WriteFile(path, []byte(yaml), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	b := cfg.Behavior
	if b.ReadSeconds != (Range{9, 9}) || b.ActionDelayMs != (Range{600, 2200}) || b.CooldownHours != 6 ||
		b.NavIntervalSeconds != (Range{8, 15}) || b.NavPerMinute != -1 {
		t.Fatalf("behavior: %+v", b)
	}
	if ch := cfg.Channel; ch.TimeoutSeconds != 600 || ch.MaxScrolls != 5 {
		t.Fatalf("channel defaults: %+v", ch)
	}
}

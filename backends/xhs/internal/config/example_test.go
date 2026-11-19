package config

import "testing"

// config.example.yaml 必须始终能被加载（注释掉的各平台示例不影响解析）。
func TestExampleConfigLoads(t *testing.T) {
	cfg, err := Load("../../config.example.yaml")
	if err != nil {
		t.Fatal(err)
	}
	b := cfg.Browser
	if b.CDPURL != "" || b.Silent || len(b.CDPLaunch) != 0 || !b.Headless || b.HeadlessSet == nil || !*b.HeadlessSet {
		t.Fatalf("示例配置应显式 headless: true: %+v", b)
	}
}

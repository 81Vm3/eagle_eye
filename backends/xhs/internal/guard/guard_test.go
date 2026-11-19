package guard

import (
	"context"
	"testing"
	"time"
)

type memKV map[string]string

func (m memKV) GetKV(_ context.Context, k string) (string, error) { return m[k], nil }
func (m memKV) SetKV(_ context.Context, k, v string) error        { m[k] = v; return nil }

func TestCooldownFor(t *testing.T) {
	for streak, want := range map[int]time.Duration{0: 6 * time.Hour, 1: 12 * time.Hour, 2: 24 * time.Hour,
		3: 48 * time.Hour, 7: 48 * time.Hour} {
		if got := CooldownFor(6, streak); got != want {
			t.Errorf("streak %d: %v want %v", streak, got, want)
		}
	}
}

func TestEnterCooldownAndCheck(t *testing.T) {
	ctx := context.Background()
	kv := memKV{}
	now := time.Date(2026, 11, 19, 10, 0, 0, 0, time.Local)
	if u, _ := CooldownUntil(ctx, kv, now); !u.IsZero() {
		t.Fatal("初始不应冷却")
	}
	u1, err := EnterCooldown(ctx, kv, 6, now)
	if err != nil || !u1.Equal(now.Add(6*time.Hour)) || kv[KeyCooldownStreak] != "1" {
		t.Fatalf("first: %v %v %v", u1, err, kv)
	}
	if u, _ := CooldownUntil(ctx, kv, now.Add(time.Hour)); !u.Equal(u1) {
		t.Fatalf("冷却中应返回截止时间: %v", u)
	}
	if u, _ := CooldownUntil(ctx, kv, now.Add(7*time.Hour)); !u.IsZero() {
		t.Fatal("过期后不应冷却")
	}
	u2, _ := EnterCooldown(ctx, kv, 6, now)
	if !u2.Equal(now.Add(12*time.Hour)) || kv[KeyCooldownStreak] != "2" {
		t.Fatalf("连续触发应翻倍: %v", u2)
	}
	if err := ResetCooldownStreak(ctx, kv); err != nil || kv[KeyCooldownStreak] != "0" {
		t.Fatalf("reset: %v %v", err, kv)
	}
	if u3, _ := EnterCooldown(ctx, kv, 6, now); !u3.Equal(now.Add(6 * time.Hour)) {
		t.Fatalf("清零后应从基础时长算起: %v", u3)
	}
	kv[KeyCooldownUntil] = "garbage"
	if u, _ := CooldownUntil(ctx, kv, now); !u.IsZero() {
		t.Fatal("格式错误按不冷却")
	}
}

func TestSeconds(t *testing.T) {
	for d, want := range map[time.Duration]int{0: 1, time.Millisecond: 1, time.Second: 1, 1500 * time.Millisecond: 2, 90 * time.Second: 90} {
		if got := Seconds(d); got != want {
			t.Errorf("Seconds(%v)=%d want %d", d, got, want)
		}
	}
}

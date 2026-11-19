// Package guard 防封状态的共享逻辑：冷却熔断、跨进程页面加载节流的键名与判断。
// 每次 channel search 都是一个新进程，所有状态都持久化在同一个
// SQLite（store 的 kv 表）里，各进程读写同一组键。
package guard

import (
	"context"
	"fmt"
	"time"
)

// kv 表的键。
const (
	// KeyCooldownUntil 冷却截止时间（RFC3339）。触发验证页后写入，冷却期内任何进程都不访问页面。
	KeyCooldownUntil = "cooldown_until"
	// KeyCooldownStreak 连续触发次数，冷却时长按它翻倍。一次完整成功的搜索会把它清零。
	KeyCooldownStreak = "cooldown_streak"
	// KeyLastNav 最近一次（已预约的）页面导航时刻（RFC3339Nano），新进程据此遵守 nav_interval_seconds。
	KeyLastNav = "last_nav_at"
)

// MaxCooldown 冷却时长上限。
const MaxCooldown = 48 * time.Hour

// KV store.Store 的 kv 读写。
type KV interface {
	GetKV(ctx context.Context, key string) (string, error)
	SetKV(ctx context.Context, key, value string) error
}

// CooldownFor 第 streak 次（从 0 起）连续触发时的冷却时长：base × 2^streak，最多 48 小时。
func CooldownFor(baseHours float64, streak int) time.Duration {
	d := time.Duration(baseHours * float64(time.Hour))
	for i := 0; i < streak && d < MaxCooldown; i++ {
		d *= 2
	}
	return min(d, MaxCooldown)
}

// EnterCooldown 触发验证页后进入冷却：按连续触发次数翻倍，写入截止时间并把连续次数加一。
func EnterCooldown(ctx context.Context, kv KV, baseHours float64, now time.Time) (time.Time, error) {
	streak := 0
	if s, _ := kv.GetKV(ctx, KeyCooldownStreak); s != "" {
		fmt.Sscan(s, &streak)
	}
	until := now.Add(CooldownFor(baseHours, streak))
	if err := kv.SetKV(ctx, KeyCooldownUntil, until.Format(time.RFC3339)); err != nil {
		return until, err
	}
	return until, kv.SetKV(ctx, KeyCooldownStreak, fmt.Sprint(streak+1))
}

// ResetCooldownStreak 一次完整成功的操作后清零连续触发次数（下次触发冷却从基础时长算起）。
func ResetCooldownStreak(ctx context.Context, kv KV) error {
	if s, err := kv.GetKV(ctx, KeyCooldownStreak); err != nil || s == "" || s == "0" {
		return err
	}
	return kv.SetKV(ctx, KeyCooldownStreak, "0")
}

// CooldownUntil 冷却截止时间；不在冷却期（没有记录、格式错误或已过期）返回零值。
func CooldownUntil(ctx context.Context, kv KV, now time.Time) (time.Time, error) {
	s, err := kv.GetKV(ctx, KeyCooldownUntil)
	if err != nil || s == "" {
		return time.Time{}, err
	}
	until, err := time.Parse(time.RFC3339, s)
	if err != nil || !now.Before(until) {
		return time.Time{}, nil
	}
	return until, nil
}

// GetTime 读取一个时间类 kv（RFC3339 / RFC3339Nano 均可）；没有或格式错误返回零值。
func GetTime(ctx context.Context, kv KV, key string) (time.Time, error) {
	s, err := kv.GetKV(ctx, key)
	if err != nil || s == "" {
		return time.Time{}, err
	}
	t, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		return time.Time{}, nil
	}
	return t, nil
}

// SetTime 写入一个时间类 kv（RFC3339Nano）。
func SetTime(ctx context.Context, kv KV, key string, t time.Time) error {
	return kv.SetKV(ctx, key, t.Format(time.RFC3339Nano))
}

// Seconds 向上取整到秒，至少 1。用于 retry_after_seconds。
func Seconds(d time.Duration) int {
	s := int((d + time.Second - 1) / time.Second)
	return max(1, s)
}

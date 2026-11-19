package xhs

import (
	"context"
	"sync"
	"testing"
	"time"
)

// fakePacer 用固定的"当前时间"，记录每次预约到的时刻（不真的睡）。
func fakePacer(minGap, maxGap time.Duration, perMinute int) (*Pacer, *[]time.Time, time.Time) {
	p := NewPacer(minGap, maxGap, perMinute)
	t0 := time.Date(2026, 1, 1, 10, 0, 0, 0, time.UTC)
	var mu sync.Mutex
	var slots []time.Time
	p.now = func() time.Time { return t0 }
	p.sleep = func(ctx context.Context, d time.Duration) error {
		mu.Lock()
		slots = append(slots, t0.Add(d))
		mu.Unlock()
		return nil
	}
	return p, &slots, t0
}

func TestPacerGapAndWindow(t *testing.T) {
	p, slots, _ := fakePacer(8*time.Second, 15*time.Second, 3)
	var wg sync.WaitGroup
	for range 10 { // 10 个标签页同时要加载
		wg.Add(1)
		go func() { defer wg.Done(); _ = p.Wait(context.Background()) }()
	}
	wg.Wait()
	s := *slots
	if len(s) != 10 {
		t.Fatalf("got %d", len(s))
	}
	sortTimes(s)
	for i := 1; i < len(s); i++ {
		if gap := s[i].Sub(s[i-1]); gap < 8*time.Second {
			t.Fatalf("间隔 %v < 8s", gap)
		}
	}
	for i := range s { // 任意 60 秒窗口内不超过 3 次
		n := 0
		for j := range s {
			if !s[j].Before(s[i]) && s[j].Before(s[i].Add(time.Minute)) {
				n++
			}
		}
		if n > 3 {
			t.Fatalf("从 %v 起 60 秒内 %d 次", s[i], n)
		}
	}
}

func TestPacerNilAndCancel(t *testing.T) {
	var p *Pacer
	if err := p.Wait(context.Background()); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := NewPacer(time.Hour, time.Hour, 1).Wait(ctx); err == nil {
		t.Fatal("取消的 ctx 应返回错误")
	}
}

func sortTimes(s []time.Time) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j].Before(s[j-1]); j-- {
			s[j], s[j-1] = s[j-1], s[j]
		}
	}
}

func TestFocusNilIsNoop(t *testing.T) {
	var f *Focus
	release, err := f.Hold(context.Background(), nil, NewHuman())
	if err != nil {
		t.Fatal(err)
	}
	release()
}

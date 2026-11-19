package xhs

import (
	"context"
	"testing"
	"time"
)

func TestNoteTimeFromID(t *testing.T) {
	now := time.Date(2026, 11, 19, 0, 0, 0, 0, time.UTC)
	// 真实笔记：详情页 note.time = 2026-11-10 15:20:00 UTC
	got, ok := NoteTimeFromID("6af336200000000009022f96", now)
	if !ok || !got.Equal(time.Date(2026, 11, 10, 15, 20, 0, 0, time.UTC)) {
		t.Fatalf("got %v %v", got, ok)
	}
	for _, bad := range []string{"", "abc", "zzzzzzzz0000000000000000", "000000010000000000000000", // 1970
		"7fffffff0000000000000000", // 2038，晚于 now+1d
		"6af33620000000000902"} {
		if _, ok := NoteTimeFromID(bad, now); ok {
			t.Errorf("%q 不应解析成功", bad)
		}
	}
}

func TestPacerSeedAndPersist(t *testing.T) {
	p, slots, t0 := fakePacer(8*time.Second, 8*time.Second, -1)
	var saved []time.Time
	p.OnReserve(func(x time.Time) { saved = append(saved, x) })
	p.Seed(t0.Add(-3 * time.Second))  // 另一个进程 3 秒前刚导航过
	p.Seed(t0.Add(-60 * time.Second)) // 更早的记录不覆盖
	if err := p.Wait(context.Background()); err != nil {
		t.Fatal(err)
	}
	if want := t0.Add(5 * time.Second); (*slots)[0] != want {
		t.Fatalf("第一次导航应等到 %v，实际 %v", want, (*slots)[0])
	}
	if len(saved) != 1 || saved[0] != t0.Add(5*time.Second) {
		t.Fatalf("persisted %v", saved)
	}

	// 很久以前的导航不影响
	q, slots2, t1 := fakePacer(8*time.Second, 8*time.Second, -1)
	q.Seed(t1.Add(-time.Hour))
	_ = q.Wait(context.Background())
	if (*slots2)[0] != t1 {
		t.Fatalf("不应等待: %v", (*slots2)[0])
	}
	// 时钟被调过、记录在遥远的未来：按刚刚导航过处理
	r, slots3, t2 := fakePacer(8*time.Second, 8*time.Second, -1)
	r.Seed(t2.Add(10 * time.Hour))
	_ = r.Wait(context.Background())
	if (*slots3)[0] != t2.Add(8*time.Second) {
		t.Fatalf("未来时刻应被钳制: %v", (*slots3)[0])
	}
	var nilP *Pacer
	nilP.Seed(t1)
	nilP.OnReserve(nil)
}

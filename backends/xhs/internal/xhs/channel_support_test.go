package xhs

import (
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

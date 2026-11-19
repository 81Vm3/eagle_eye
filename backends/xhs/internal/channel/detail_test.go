package channel

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"eagle-eye/xhs/internal/xhs"
)

func mkCards(n int) []xhs.Card {
	cs := make([]xhs.Card, n)
	for i := range cs {
		cs[i] = xhs.Card{ID: fmt.Sprintf("n%d", i)}
	}
	return cs
}

func TestFetchDetailsConcurrencyCap(t *testing.T) {
	for _, limit := range []int{1, 3, 10} {
		var cur, peak int32
		res, err := fetchDetails(context.Background(), mkCards(8), limit, func(ctx context.Context, c xhs.Card) (*xhs.Note, error) {
			n := atomic.AddInt32(&cur, 1)
			for {
				p := atomic.LoadInt32(&peak)
				if n <= p || atomic.CompareAndSwapInt32(&peak, p, n) {
					break
				}
			}
			time.Sleep(10 * time.Millisecond)
			atomic.AddInt32(&cur, -1)
			if c.ID == "n5" {
				return nil, xhs.ErrNoteUnavailable // 普通错误只影响这一篇
			}
			return &xhs.Note{NoteID: c.ID}, nil
		})
		if err != nil {
			t.Fatal(err)
		}
		if want := int32(min(limit, 8)); peak != want {
			t.Errorf("limit=%d: 峰值并发 %d，应为 %d", limit, peak, want)
		}
		for i, r := range res {
			if i == 5 {
				if !errors.Is(r.Err, xhs.ErrNoteUnavailable) || r.Note != nil {
					t.Errorf("n5: %+v", r)
				}
			} else if r.Note == nil || r.Note.NoteID != fmt.Sprintf("n%d", i) {
				t.Errorf("结果顺序应与输入一致: %d %+v", i, r)
			}
		}
	}
}

// 任一篇遇到验证页：其余进行中的读取被取消，不再启动新的读取，返回 ErrVerification。
func TestFetchDetailsAbortOnVerification(t *testing.T) {
	var started, cancelled int32
	var mu sync.Mutex
	seen := map[string]bool{}
	_, err := fetchDetails(context.Background(), mkCards(10), 3, func(ctx context.Context, c xhs.Card) (*xhs.Note, error) {
		atomic.AddInt32(&started, 1)
		mu.Lock()
		seen[c.ID] = true
		mu.Unlock()
		if c.ID == "n1" {
			time.Sleep(5 * time.Millisecond)
			return nil, xhs.ErrVerification
		}
		select { // 其他标签页一直加载，直到被取消
		case <-ctx.Done():
			atomic.AddInt32(&cancelled, 1)
			return nil, ctx.Err()
		case <-time.After(5 * time.Second):
			return &xhs.Note{NoteID: c.ID}, nil
		}
	})
	if !errors.Is(err, xhs.ErrVerification) {
		t.Fatalf("got %v", err)
	}
	if started != 3 || cancelled != 2 {
		t.Fatalf("应只启动前 3 篇并取消其余 2 篇: started=%d cancelled=%d %v", started, cancelled, seen)
	}
}

func TestFetchDetailsParentCancel(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	_, err := fetchDetails(ctx, mkCards(4), 2, func(ctx context.Context, c xhs.Card) (*xhs.Note, error) {
		<-ctx.Done()
		return nil, ctx.Err()
	})
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("got %v", err)
	}
	// 登录失效同样中止
	_, err = fetchDetails(context.Background(), mkCards(2), 2, func(ctx context.Context, c xhs.Card) (*xhs.Note, error) {
		return nil, xhs.ErrLoginRequired
	})
	if !errors.Is(err, xhs.ErrLoginRequired) {
		t.Fatal(err)
	}
	if res, err := fetchDetails(context.Background(), nil, 3, nil); err != nil || len(res) != 0 {
		t.Fatal(res, err)
	}
}

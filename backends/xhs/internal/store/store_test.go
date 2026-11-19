package store

import (
	"context"
	"fmt"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestStoreRoundTrip(t *testing.T) {
	ctx := context.Background()
	s, err := Open(filepath.Join(t.TempDir(), "t.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	if v, err := s.GetKV(ctx, "missing"); err != nil || v != "" {
		t.Fatalf("missing kv: %q %v", v, err)
	}
	_ = s.SetKV(ctx, "k", "v1")
	_ = s.SetKV(ctx, "k", "v2")
	if v, _ := s.GetKV(ctx, "k"); v != "v2" {
		t.Fatalf("kv %q", v)
	}
}

// login 与 channel search（或两个 channel search）是两个进程、两个连接池同时开同一个库：WAL + busy_timeout 下
// 并发写 kv 不能报 database is locked，也不能丢写。
func TestKVAcrossConnections(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "shared.db")
	a, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer a.Close()
	b, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer b.Close()

	var mode string
	if err := a.db.QueryRow(`PRAGMA journal_mode`).Scan(&mode); err != nil || mode != "wal" {
		t.Fatalf("journal_mode=%q err=%v", mode, err)
	}
	var busy int
	if err := b.db.QueryRow(`PRAGMA busy_timeout`).Scan(&busy); err != nil || busy < 5000 {
		t.Fatalf("busy_timeout=%d err=%v", busy, err)
	}

	const n = 60
	var wg sync.WaitGroup
	var mu sync.Mutex
	var errs []error
	for i := range n {
		s := a
		if i%2 == 1 {
			s = b
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			err := s.SetKV(ctx, fmt.Sprintf("k%d", i), "v")
			if err == nil {
				err = s.SetKV(ctx, "test_key", time.Now().Format(time.RFC3339Nano))
			}
			if err != nil {
				mu.Lock()
				errs = append(errs, err)
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	if len(errs) > 0 {
		t.Fatalf("并发写出错: %v", errs[0])
	}
	var rows int
	if err := b.db.QueryRow(`SELECT COUNT(*) FROM kv`).Scan(&rows); err != nil || rows != n+1 {
		t.Fatalf("kv rows=%d err=%v want %d", rows, err, n+1)
	}
}

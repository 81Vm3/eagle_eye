package lock

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestTryLockExclusive(t *testing.T) {
	path := filepath.Join(t.TempDir(), "sub", "browser.lock")
	a, err := TryLock(path, "login")
	if err != nil {
		t.Fatal(err)
	}
	// flock 锁属于打开的文件描述，同一进程里第二次打开也会冲突（等价于另一个进程）
	_, err = TryLock(path, "channel search")
	if !errors.Is(err, ErrLocked) {
		t.Fatalf("第二次加锁应失败: %v", err)
	}
	var le *LockedError
	if !errors.As(err, &le) || !strings.Contains(le.Holder, fmt.Sprintf("pid=%d", os.Getpid())) || !strings.Contains(le.Holder, "cmd=login") {
		t.Fatalf("应带持有者信息: %+v", le)
	}
	if err := a.Unlock(); err != nil {
		t.Fatal(err)
	}
	b, err := TryLock(path, "channel search")
	if err != nil {
		t.Fatalf("释放后应能再加锁: %v", err)
	}
	_ = b.Unlock()
	var nilLock *Lock
	if err := nilLock.Unlock(); err != nil {
		t.Fatal(err)
	}
}

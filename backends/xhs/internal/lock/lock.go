// Package lock 浏览器互斥：用文件锁（flock）保证 login / channel search
// 任何时刻只有一个进程在驱动浏览器。锁随进程退出（包括被 kill）由内核自动释放，不会残留。
package lock

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// ErrLocked 锁正被另一个进程持有。
var ErrLocked = errors.New("浏览器正被另一个进程使用")

// LockedError 带持有者信息的 ErrLocked。
type LockedError struct {
	Path   string
	Holder string // 持有者写入的说明（pid / 命令 / 开始时间），可能为空
}

func (e *LockedError) Error() string {
	if e.Holder == "" {
		return ErrLocked.Error()
	}
	return fmt.Sprintf("%s（%s）", ErrLocked.Error(), e.Holder)
}

func (e *LockedError) Unwrap() error { return ErrLocked }

// Lock 已持有的锁。
type Lock struct {
	f *os.File
}

// TryLock 非阻塞地获取 path 上的排他锁；被占用时立即返回 *LockedError（errors.Is(err, ErrLocked)）。
// owner 写进锁文件，供被拒绝的进程提示是谁在用。
//
// 文件由 os.OpenFile 打开，Go 默认带 O_CLOEXEC：cdp_launch 拉起的 Chrome 不会继承这个 fd，
// 否则 Chrome 常驻会让锁永远无法释放。
func TryLock(path, owner string) (*Lock, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o644)
	if err != nil {
		return nil, err
	}
	if err := flock(f); err != nil {
		f.Close()
		if errors.Is(err, ErrLocked) {
			return nil, &LockedError{Path: path, Holder: Holder(path)}
		}
		return nil, fmt.Errorf("获取浏览器锁 %s: %w", path, err)
	}
	info := fmt.Sprintf("pid=%d cmd=%s since=%s", os.Getpid(), owner, time.Now().Format("01-02 15:04:05"))
	if err := f.Truncate(0); err == nil {
		_, _ = f.WriteAt([]byte(info+"\n"), 0)
	}
	return &Lock{f: f}, nil
}

// Unlock 释放锁。nil 安全。
func (l *Lock) Unlock() error {
	if l == nil || l.f == nil {
		return nil
	}
	_ = l.f.Truncate(0)
	err := funlock(l.f)
	l.f.Close()
	l.f = nil
	return err
}

// Holder 读取锁文件里持有者写入的说明。
func Holder(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

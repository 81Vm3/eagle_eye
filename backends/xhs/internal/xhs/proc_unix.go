//go:build unix

package xhs

import (
	"errors"
	"syscall"
)

// processAlive pid 对应的进程是否还在（无权限发信号也说明进程存在）。
func processAlive(pid int) bool {
	err := syscall.Kill(pid, 0)
	return err == nil || errors.Is(err, syscall.EPERM)
}

// lockfileBusy 只有 Windows 的 Chrome 用 lockfile，unix 上看 SingletonLock。
func lockfileBusy(string) bool { return false }

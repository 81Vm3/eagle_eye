//go:build windows

package xhs

import (
	"errors"
	"io/fs"
	"os"
	"syscall"
)

// processAlive 能以查询权限打开进程且尚未退出时为 true。
func processAlive(pid int) bool {
	const queryLimited = 0x1000 // PROCESS_QUERY_LIMITED_INFORMATION
	h, err := syscall.OpenProcess(queryLimited|syscall.SYNCHRONIZE, false, uint32(pid))
	if err != nil {
		return false
	}
	defer syscall.CloseHandle(h)
	ev, err := syscall.WaitForSingleObject(h, 0)
	return err == nil && ev == uint32(syscall.WAIT_TIMEOUT)
}

// lockfileBusy Windows 上运行中的 Chrome 独占打开 <profile>\lockfile：能打开说明没人占用。
func lockfileBusy(path string) bool {
	f, err := os.OpenFile(path, os.O_RDWR, 0)
	if err == nil {
		f.Close()
		return false
	}
	return !errors.Is(err, fs.ErrNotExist)
}

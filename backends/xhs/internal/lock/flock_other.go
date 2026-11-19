//go:build !unix

package lock

import (
	"errors"
	"os"
)

// 非 unix 平台不支持（本项目运行在 Linux / WSL / macOS）。拒绝而不是假装加锁成功。
func flock(*os.File) error { return errors.New("当前平台不支持浏览器文件锁") }

func funlock(*os.File) error { return nil }

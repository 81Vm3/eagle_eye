//go:build !unix

package xhs

import "os/exec"

// detach 非 unix（Windows）：子进程默认就不随父进程退出，保持原行为。
func detach(*exec.Cmd) {}

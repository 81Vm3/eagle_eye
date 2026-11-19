//go:build unix

package xhs

import (
	"os/exec"
	"syscall"
)

// detach 让 Chrome 在新会话里运行：终端关闭（SIGHUP）或 Ctrl-C 发给本进程组时不会连带杀掉它。
func detach(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
}

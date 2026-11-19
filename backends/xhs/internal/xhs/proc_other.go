//go:build !unix && !windows

package xhs

func processAlive(int) bool    { return false }
func lockfileBusy(string) bool { return false }

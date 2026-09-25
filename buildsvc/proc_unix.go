//go:build !windows

package buildsvc

import (
	"os/exec"
	"syscall"
)

// prepareCmd puts the command in its own process group so a timeout can kill
// the whole process tree, not only the shell.
func prepareCmd(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

// killProcessGroup signals the entire group (negative pid).
func killProcessGroup(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
}

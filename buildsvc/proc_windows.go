//go:build windows

package buildsvc

import "os/exec"

// prepareCmd is a no-op on Windows.
func prepareCmd(cmd *exec.Cmd) {}

// killProcessGroup kills just the process on Windows.
func killProcessGroup(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	return cmd.Process.Kill()
}

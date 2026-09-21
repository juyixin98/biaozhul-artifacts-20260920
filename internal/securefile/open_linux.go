//go:build linux

package securefile

import (
	"os"
	"syscall"
)

// openHardened opens the path read-only and refuses a symlink at the final
// component (intermediate components were already resolved by EvalSymlinks).
func openHardened(real string) (*os.File, error) {
	fd, err := syscall.Open(real, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		if err == syscall.ELOOP {
			return nil, ErrSymlinkEscape
		}
		return nil, &os.PathError{Op: "open", Path: real, Err: err}
	}
	return os.NewFile(uintptr(fd), real), nil
}

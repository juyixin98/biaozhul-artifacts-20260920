//go:build linux

package apply

import (
	"fmt"
	"syscall"
)

type statfsChecker struct{}

func (statfsChecker) AvailableBytes(dir string) (int64, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(dir, &st); err != nil {
		return 0, fmt.Errorf("statfs %s: %w", dir, err)
	}
	return int64(st.Bavail) * int64(st.Bsize), nil
}

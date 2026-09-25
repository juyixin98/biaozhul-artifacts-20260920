//go:build !linux

package apply

import (
	"errors"
	"os"
)

type statfsChecker struct{}

// AvailableBytes falls back to a permissive check off Linux: no statfs is
// attempted, and a zero-length read of the directory serves as a sanity probe.
func (statfsChecker) AvailableBytes(dir string) (int64, error) {
	if _, err := os.Stat(dir); err != nil {
		return 0, err
	}
	return 1 << 40, nil // assume 1 TiB
}

var _ = errors.New

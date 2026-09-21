//go:build linux

package securefile

import (
	"fmt"
	"os"
)

// fdRealPath resolves the kernel-visible path of an open descriptor via
// /proc/self/fd. A returned mismatch with the pre-open resolution means the
// path namespace changed (symlink swap) between EvalSymlinks and open.
func fdRealPath(f *os.File) (string, error) {
	p, err := os.Readlink(fmt.Sprintf("/proc/self/fd/%d", f.Fd()))
	if err != nil {
		return "", err
	}
	return p, nil
}

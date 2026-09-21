//go:build !linux

package securefile

import "os"

// fdRealPath is a no-op on non-Linux platforms; the EvalSymlinks pre-check and
// post-open identity re-check remain in effect.
func fdRealPath(f *os.File) (string, error) {
	return "", nil
}

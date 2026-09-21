//go:build !linux

package securefile

import "os"

// openHardened falls back to a plain read-only open on platforms without
// O_NOFOLLOW. The pre-open EvalSymlinks + post-open identity re-check still
// apply; ForensicCore itself is deployed on Linux.
func openHardened(real string) (*os.File, error) {
	return os.Open(real)
}

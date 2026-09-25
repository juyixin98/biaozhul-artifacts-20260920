//go:build !linux

package apply

import "os"

// Minimal off-Linux lock: exclusive O_CREATE|O_EXCL lock file. It is NOT
// auto-released on SIGKILL (unlike flock); tests exercising hard crashes run
// on Linux.
type targetLock struct {
	path string
	f    *os.File
}

func acquireTargetLock(path string) (*targetLock, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_RDWR, 0o600)
	if err != nil {
		if os.IsExist(err) {
			return nil, ErrLocked
		}
		return nil, err
	}
	return &targetLock{path: path, f: f}, nil
}

func (l *targetLock) release() error {
	defer os.Remove(l.path)
	return l.f.Close()
}

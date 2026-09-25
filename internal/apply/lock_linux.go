//go:build linux

package apply

import (
	"os"
	"syscall"
)

// targetLock is an advisory, whole-process-exclusive lock held while a patch
// is being applied to one target path. It is released on process exit even if
// the process is killed (kernel-owned flock), so a hard crash never wedges the
// lock; the lock file itself may remain on disk and is harmlessly recreated.
type targetLock struct {
	f *os.File
}

func acquireTargetLock(path string) (*targetLock, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		if err == syscall.EWOULDBLOCK {
			return nil, ErrLocked
		}
		return nil, err
	}
	return &targetLock{f: f}, nil
}

func (l *targetLock) release() error {
	defer os.Remove(l.f.Name())
	_ = syscall.Flock(int(l.f.Fd()), syscall.LOCK_UN)
	return l.f.Close()
}

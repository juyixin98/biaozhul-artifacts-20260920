//go:build linux

package securefile

import (
	"os"
	"syscall"
	"time"
)

func fdIdentity(f *os.File) (Identity, error) {
	var st syscall.Stat_t
	if err := syscall.Fstat(int(f.Fd()), &st); err != nil {
		return Identity{}, &os.PathError{Op: "fstat", Path: f.Name(), Err: err}
	}
	mode := os.FileMode(st.Mode & 0o777)
	switch st.Mode & syscall.S_IFMT {
	case syscall.S_IFDIR:
		mode |= os.ModeDir
	case syscall.S_IFLNK:
		mode |= os.ModeSymlink
	case syscall.S_IFBLK, syscall.S_IFCHR:
		mode |= os.ModeDevice
	case syscall.S_IFIFO:
		mode |= os.ModeNamedPipe
	case syscall.S_IFSOCK:
		mode |= os.ModeSocket
	}
	return Identity{
		Size:     st.Size,
		Mode:     mode,
		ModTime:  time.Unix(st.Mtim.Sec, st.Mtim.Nsec),
		CTime:    time.Unix(st.Ctim.Sec, st.Ctim.Nsec),
		DeviceID: st.Dev,
		Inode:    st.Ino,
	}, nil
}

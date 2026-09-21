package securefile

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"time"
)

var (
	// ErrOutsideWhitelist is returned when a path (after full symlink
	// resolution) is not inside a configured whitelist root.
	ErrOutsideWhitelist = errors.New("path escapes the evidence whitelist directories")
	// ErrSymlinkEscape is returned when a symlink component resolves outside
	// the whitelist or swaps during the open race.
	ErrSymlinkEscape = errors.New("symlink resolves outside the evidence whitelist")
	// ErrNotRegularFile is returned for directories, devices, sockets, etc.
	ErrNotRegularFile = errors.New("path is not a regular file")
	// ErrUnsupportedType is returned for anything that is not a .raw/.dd image.
	ErrUnsupportedType = errors.New("only .raw and .dd image files are accepted")
	// ErrFileChanged means the file's size, identity or mtime changed while it
	// was being read.
	ErrFileChanged = errors.New("file changed while being read; refusing to persist a baseline")
)

// Identity captures the stable kernel-visible attributes of an open file.
type Identity struct {
	RealPath string
	Size     int64
	Mode     os.FileMode
	ModTime  time.Time
	CTime    time.Time // inode change time: advances on any content/metadata write
	DeviceID uint64
	Inode    uint64
}

// Equal reports whether two identities describe the same unchanged file.
// Name and path are deliberately NOT part of identity: a rename or replacement
// at the same path must be detected via device/inode, size and times. Ctime is
// included because a fast in-place rewrite can leave mtime within the same
// clock tick as the starting snapshot.
func (i Identity) Equal(o Identity) bool {
	return i.Size == o.Size &&
		i.DeviceID == o.DeviceID &&
		i.Inode == o.Inode &&
		i.ModTime.UnixNano() == o.ModTime.UnixNano() &&
		i.CTime.UnixNano() == o.CTime.UnixNano()
}

// HasRawExt reports whether name ends in .raw or .dd (case-insensitive).
func HasRawExt(name string) bool {
	ext := strings.ToLower(filepath.Ext(name))
	return ext == ".raw" || ext == ".dd"
}

// within performs the separator-aware prefix test on real (symlink-free) paths.
func within(realPath, root string) bool {
	if realPath == root {
		return true
	}
	rel, err := filepath.Rel(root, realPath)
	if err != nil {
		return false
	}
	if rel == "." {
		return true
	}
	if rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) ||
		filepath.IsAbs(rel) {
		return false
	}
	return true
}

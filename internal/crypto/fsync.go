package crypto

import "os"

// FsyncDir flushes directory metadata (needed after rename/link on Linux).
func FsyncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}

package apply

import (
	"fmt"
	"os"
	"path/filepath"
)

// fsyncDir fsyncs a directory so a rename inside it is durable on crash.
func fsyncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return fmt.Errorf("fsync dir: %w", err)
	}
	defer d.Close()
	if err := d.Sync(); err != nil {
		return fmt.Errorf("fsync dir: %w", err)
	}
	return nil
}

// tempPath is the staging path for target.
func tempPath(target string) string {
	return target + ".delta.tmp"
}

// lockPath is the per-target advisory lock path (sibling of the target).
func lockPath(target string) string {
	return target + ".delta.lock"
}

func parentDir(target string) string {
	d := filepath.Dir(target)
	if d == "" {
		return "."
	}
	return d
}

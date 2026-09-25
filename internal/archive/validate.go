package archive

import (
	"fmt"
	"path/filepath"
)

// ValidateTree walks root with Lstat semantics and returns an error if it
// contains a symlink that escapes root (or a symlink loop). It is safe to
// call before copying or archiving an untrusted source tree.
func ValidateTree(root string) error {
	l := osLister{}
	absRoot, err := filepath.Abs(root)
	if err != nil {
		return fmt.Errorf("archive: resolve root: %w", err)
	}
	_, err = collect(l, absRoot)
	return err
}

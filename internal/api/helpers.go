package api

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
)

var namePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)

func nameOK(name string) bool { return namePattern.MatchString(name) }

func readDirNames(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var names []string
	for _, e := range entries {
		if e.IsDir() {
			names = append(names, e.Name())
		}
	}
	return names, nil
}

func removeAll(p string) error { return os.RemoveAll(p) }

// importTree copies a local fixture directory into the store's projects
// tree. The destination must not exist unless replace is set. Symlinks are
// rejected: project trees must be plain files/directories so that path
// containment checks and digest recomputation are unambiguous.
func importTree(srcDir, dst string, replace bool) error {
	src, err := filepath.Abs(srcDir)
	if err != nil {
		return err
	}
	info, err := os.Stat(src)
	if err != nil {
		return fmt.Errorf("source dir: %w", err)
	}
	if !info.IsDir() {
		return fmt.Errorf("source %q is not a directory", src)
	}
	if _, err := os.Stat(dst); err == nil {
		if !replace {
			return fmt.Errorf("destination already exists")
		}
		if err := os.RemoveAll(dst); err != nil {
			return err
		}
	} else if !os.IsNotExist(err) {
		return err
	}
	if err := os.MkdirAll(dst, 0o755); err != nil {
		return err
	}
	return filepath.Walk(src, func(path string, fi os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		target := filepath.Join(dst, rel)
		switch {
		case fi.IsDir():
			return os.MkdirAll(target, fi.Mode().Perm())
		case fi.Mode()&os.ModeSymlink != 0:
			return fmt.Errorf("symlinks are not allowed in project trees: %s", rel)
		case fi.Mode().IsRegular():
			in, err := os.Open(path)
			if err != nil {
				return err
			}
			defer in.Close()
			out, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, fi.Mode().Perm())
			if err != nil {
				return fmt.Errorf("copy %s: %w", rel, err)
			}
			if _, err := io.Copy(out, in); err != nil {
				out.Close()
				return err
			}
			return out.Close()
		default:
			return fmt.Errorf("unsupported file type: %s", rel)
		}
	})
}

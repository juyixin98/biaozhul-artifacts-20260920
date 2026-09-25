package main

import (
	"fmt"
	"io"
	"os"

	"deltaupdate/internal/store"
)

// cacheFromFlags parses a small flag set looking for --cache and opens the store.
func openStore(cacheDir string) (*store.Store, error) {
	if cacheDir == "" {
		return nil, fmt.Errorf("--cache is required")
	}
	return store.Open(cacheDir)
}

func copyFileToStore(st *store.Store, path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	return st.Put(f, "")
}

func writeFileAtomic(path string, r io.Reader) error {
	tmp := path + ".tmp"
	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	if _, err := io.Copy(f, r); err != nil {
		f.Close()
		os.Remove(tmp)
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		os.Remove(tmp)
		return err
	}
	if err := f.Close(); err != nil {
		os.Remove(tmp)
		return err
	}
	return os.Rename(tmp, path)
}

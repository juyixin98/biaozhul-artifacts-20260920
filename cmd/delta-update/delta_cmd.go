package main

import (
	"bytes"
	"flag"
	"fmt"
	"os"

	"deltaupdate/internal/delta"
)

// delta --cache DIR --old REF --new REF [--block-size N] [--patch FILE]
func runDelta(args []string) error {
	fs := flag.NewFlagSet("delta", flag.ContinueOnError)
	cache := fs.String("cache", "", "cache directory")
	oldRef := fs.String("old", "", "old artifact ref (hash or absolute path)")
	newRef := fs.String("new", "", "new artifact ref (hash or absolute path)")
	blockSize := fs.Int("block-size", delta.DefaultBlockSize, "block size")
	patchPath := fs.String("patch", "", "write patch JSON to file (default stdout)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	st, err := openStore(*cache)
	if err != nil {
		return err
	}
	oldR, oldSize, err := st.Resolve(*oldRef)
	if err != nil {
		return fmt.Errorf("old: %w", err)
	}
	defer oldR.Close()
	newR, newSize, err := st.Resolve(*newRef)
	if err != nil {
		return fmt.Errorf("new: %w", err)
	}
	defer newR.Close()

	p, err := delta.Generate(oldR, oldSize, newR, newSize, *blockSize)
	if err != nil {
		return err
	}
	raw, err := p.Marshal()
	if err != nil {
		return err
	}
	if *patchPath == "" || *patchPath == "-" {
		_, err = os.Stdout.Write(raw)
		return err
	}
	return writeFileAtomic(*patchPath, bytes.NewReader(raw))
}

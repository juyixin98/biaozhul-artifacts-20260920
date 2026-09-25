package main

import (
	"flag"
	"fmt"
	"os"

	"deltaupdate/internal/delta"
)

// inspect FILE — prints size and sha256 of a local file.
func runInspect(args []string) error {
	fs := flag.NewFlagSet("inspect", flag.ContinueOnError)
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 1 {
		return fmt.Errorf("usage: inspect FILE")
	}
	f, err := os.Open(fs.Arg(0))
	if err != nil {
		return err
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil {
		return err
	}
	sum, err := delta.DigestReader(f)
	if err != nil {
		return err
	}
	fmt.Printf("path:   %s\nsize:   %d\nsha256: %s\n", fs.Arg(0), fi.Size(), sum)
	return nil
}

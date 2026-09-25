package main

import (
	"flag"
	"fmt"
)

// artifact put --cache DIR FILE
func runArtifactPut(args []string) error {
	fs := flag.NewFlagSet("artifact put", flag.ContinueOnError)
	cache := fs.String("cache", "", "cache directory")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 1 {
		return fmt.Errorf("usage: artifact put --cache DIR FILE")
	}
	st, err := openStore(*cache)
	if err != nil {
		return err
	}
	h, err := copyFileToStore(st, fs.Arg(0))
	if err != nil {
		return err
	}
	fmt.Println(h)
	return nil
}

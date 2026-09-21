// Command gensamples creates small deterministic evidence-image samples used
// by the docker-compose demo: a raw image and an .dd image, plus their
// SHA-256 digests printed to stdout.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
)

func main() {
	dir := "samples"
	if len(os.Args) > 1 {
		dir = os.Args[1]
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	for _, spec := range []struct {
		name string
		size int
		seed byte
	}{
		{"sample.raw", 1024 * 1024, 0xA5},
		{"sample.dd", 512 * 1024, 0x5A},
	} {
		data := make([]byte, spec.size)
		for i := range data {
			data[i] = byte(int(spec.seed) ^ (i * 31 % 251))
		}
		path := filepath.Join(dir, spec.name)
		if err := os.WriteFile(path, data, 0o644); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		sum := sha256.Sum256(data)
		fmt.Printf("%s  %d bytes  sha256=%s\n", path, spec.size, hex.EncodeToString(sum[:]))
	}
}

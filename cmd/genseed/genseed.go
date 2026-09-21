// Command genseed creates small deterministic raw/dd sample images for
// experiments and tests.
//
// Usage: genseed [output_dir]  (default /data/samples)
package main

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
)

func main() {
	dir := "/data/samples"
	if len(os.Args) > 1 {
		dir = os.Args[1]
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		fatal(err)
	}
	if err := gen(filepath.Join(dir, "sample_1mb.dd"), 1024*1024, 0x46434f52); err != nil {
		fatal(err)
	}
	if err := gen(filepath.Join(dir, "sample_256k.raw"), 256*1024, 0x45564944); err != nil {
		fatal(err)
	}
	fmt.Printf("sample images written to %s\n", dir)
}

func gen(path string, size int, seed uint32) error {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()

	h := sha256.New()
	buf := make([]byte, 4096)
	var written int
	for written < size {
		n := len(buf)
		if size-written < n {
			n = size - written
		}
		for i := 0; i+8 <= n; i += 8 {
			binary.LittleEndian.PutUint32(buf[i:i+4], seed)
			binary.LittleEndian.PutUint32(buf[i+4:i+8], uint32(written+i))
		}
		// Tail of a final, non-multiple-of-8 chunk.
		for i := n - (n % 8); i < n; i++ {
			buf[i] = byte(seed >> ((i % 4) * 8))
		}
		if _, err := f.Write(buf[:n]); err != nil {
			return err
		}
		h.Write(buf[:n])
		written += n
	}
	sum := hex.EncodeToString(h.Sum(nil))
	fmt.Printf("  %s: %d bytes sha256=%s\n", filepath.Base(path), size, sum)
	return nil
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "genseed:", err)
	os.Exit(1)
}

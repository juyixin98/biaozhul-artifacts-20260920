// mksample 生成小体积、确定性的 raw/dd 样例镜像，供手工演示与 Docker 挂载使用。
// 镜像内容由固定种子的 LCG 产生，多次生成字节完全一致，因此 SHA-256 可复现。
//
// 用法：
//
//	go run ./cmd/mksample -dir ./samples
//
// 生成后会打印每个文件的大小与 SHA-256。
package main

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
)

type sample struct {
	name string
	size int
	seed uint64
}

func main() {
	dir := flag.String("dir", "./samples", "output directory for sample images")
	flag.Parse()

	samples := []sample{
		{"disk-a.raw", 1 * 1024 * 1024, 0x12345678}, // 1 MiB
		{"disk-b.dd", 256 * 1024, 0x9ABCDEF0},       // 256 KiB
		{"sub/disk-c.raw", 3 * 4096, 0x0BADF00D},    // 12 KiB，子目录
	}

	if err := os.MkdirAll(*dir, 0o755); err != nil {
		log.Fatal(err)
	}
	for _, s := range samples {
		full := filepath.Join(*dir, s.name)
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			log.Fatal(err)
		}
		h, err := writeImage(full, s.size, s.seed)
		if err != nil {
			log.Fatal(err)
		}
		fmt.Printf("%-16s %8d bytes  sha256=%s\n", s.name, s.size, h)
	}
}

// writeImage 用 LCG 产生确定性字节并写入文件，返回大写 SHA-256。
func writeImage(path string, size int, seed uint64) (string, error) {
	f, err := os.Create(path)
	if err != nil {
		return "", err
	}
	defer f.Close()

	state := seed
	buf := make([]byte, 4096)
	h := sha256.New()
	written := 0
	for written < size {
		for i := 0; i < len(buf); i += 8 {
			state = state*6364136223846793005 + 1442695040888963407
			binary.LittleEndian.PutUint64(buf[i:i+8], state)
		}
		n := len(buf)
		if written+n > size {
			n = size - written
		}
		if _, err := io.MultiWriter(f, h).Write(buf[:n]); err != nil {
			return "", err
		}
		written += n
	}
	sum := h.Sum(nil)
	return hex.EncodeToString(sum[:]), nil
}

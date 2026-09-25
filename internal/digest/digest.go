// Package digest 提供内容摘要（SHA-256）工具，用于制品、补丁与数据块的完整性核验。
package digest

import (
	"crypto/sha256"
	"encoding/hex"
	"hash"
	"io"
	"os"
)

// Size 是 SHA-256 摘要的字节长度。
const Size = sha256.Size

// Bytes 计算一段内存数据的 SHA-256 摘要（hex 编码）。
func Bytes(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// Reader 流式计算 r 全部内容的 SHA-256 摘要（hex 编码）。
func Reader(r io.Reader) (string, error) {
	h := sha256.New()
	if _, err := io.Copy(h, r); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// File 流式计算文件的 SHA-256 摘要（hex 编码）。
func File(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	return Reader(f)
}

// TeeReader 从 r 读取的同时把读到的内容写入 w 并累计 SHA-256；
// 读完后调用 Sum 取摘要。w 可为 nil（只算摘要不落盘）。
type TeeReader struct {
	r io.Reader
	w io.Writer
	h hash.Hash
}

// NewTeeReader 构造 TeeReader。
func NewTeeReader(r io.Reader, w io.Writer) *TeeReader {
	return &TeeReader{r: r, w: w, h: sha256.New()}
}

func (t *TeeReader) Read(p []byte) (int, error) {
	n, err := t.r.Read(p)
	if n > 0 {
		t.h.Write(p[:n])
		if t.w != nil {
			if _, werr := t.w.Write(p[:n]); werr != nil {
				return n, werr
			}
		}
	}
	return n, err
}

// Sum 返回已读取内容的 SHA-256 摘要（hex 编码）。
func (t *TeeReader) Sum() string {
	return hex.EncodeToString(t.h.Sum(nil))
}

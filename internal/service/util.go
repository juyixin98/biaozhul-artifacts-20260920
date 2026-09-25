package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"hash"
	"io"
	"time"
)

// contextWithTimeout 包装 context.WithTimeout，便于测试替换。
func contextWithTimeout(d time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), d)
}

// hashWriter 把写入内容同时计入 SHA-256。
type hashWriter struct {
	w io.Writer
	h hash.Hash
}

func newHashWriter(w io.Writer) *hashWriter {
	return &hashWriter{w: w, h: sha256.New()}
}

func (hw *hashWriter) Write(p []byte) (int, error) {
	hw.h.Write(p)
	return hw.w.Write(p)
}

// Sum 返回已写入内容的 SHA-256（hex）。
func (hw *hashWriter) Sum() string {
	return hex.EncodeToString(hw.h.Sum(nil))
}

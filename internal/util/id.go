package util

import (
	"crypto/rand"
	"encoding/hex"
)

// NewID 返回 26 字符的随机十六进制 ID（ULID 风格长度，无外部依赖）。
func NewID() string {
	var b [13]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

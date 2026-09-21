// Package util 存放跨包的小工具。
package util

import (
	"crypto/rand"
	"encoding/hex"
)

// NewID 生成 16 字节随机十六进制 ID（32 字符）。
func NewID() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		// crypto/rand 失败在正常运行环境不应发生；退化为 panic 暴露问题。
		panic(err)
	}
	return hex.EncodeToString(b)
}

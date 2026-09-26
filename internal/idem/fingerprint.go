// Package idem 实现幂等键与请求摘要的绑定。
//
// 幂等键（Idempotency-Key，由调用方提供）只负责"同一请求"的归并；
// 真正防止"同键套不同请求"的是服务端计算的请求摘要 fingerprint：
// fingerprint = SHA-256(方法 + "\n" + 路径 + "\n" + 规范化正文)。
// JSON 正文先做规范化（按键名排序、去空白），因此字段顺序不同但语义
// 相同的 JSON 视为同一请求；无法解析为 JSON 时退化为对原始字节摘要，
// 即逐字节不同就算冲突。
package idem

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
)

// Fingerprint 计算 (method, path, body) 的绑定摘要，返回十六进制字符串。
func Fingerprint(method, path string, body []byte) string {
	canonical := canonicalBody(body)
	h := sha256.New()
	fmt.Fprintf(h, "%s\n%s\n", method, path)
	h.Write(canonical)
	return hex.EncodeToString(h.Sum(nil))
}

// BodyDigest 仅对正文取摘要，用于响应中展示与排查。
func BodyDigest(body []byte) string {
	h := sha256.Sum256(canonicalBody(body))
	return hex.EncodeToString(h[:])
}

// canonicalBody 尽量把 JSON 正文规范化；非 JSON 原样返回。
func canonicalBody(body []byte) []byte {
	trimmed := bytes.TrimSpace(body)
	if len(trimmed) == 0 {
		return nil
	}
	var v any
	dec := json.NewDecoder(bytes.NewReader(trimmed))
	dec.UseNumber()
	if err := dec.Decode(&v); err != nil {
		return trimmed // 非 JSON：逐字节绑定
	}
	if dec.Decode(&struct{}{}) != io.EOF {
		return trimmed // 正文含多个 JSON 值，不做规范化
	}
	out, err := json.Marshal(v) // encoding/json 会对 map 键排序
	if err != nil {
		return trimmed
	}
	return out
}

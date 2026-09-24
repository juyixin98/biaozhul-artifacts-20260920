// Package evidence 定义测试证据的签名负载与校验规则。
// 证据是不可变版本化对象：(artifact_digest, version) 唯一，
// 晋升只按 (evidence_id, version) 精确引用，绝不按“最新”之类浮动引用。
package evidence

import (
	"fmt"

	"atomicpromo/internal/crypto/canon"
	"atomicpromo/internal/crypto/sig"
)

// Envelope 提交测试证据时的请求体
type Envelope struct {
	EvidenceID     string         `json:"evidence_id"`
	Version        int64          `json:"version"`
	ArtifactDigest string         `json:"artifact_digest"`
	TestsPassed    bool           `json:"tests_passed"`
	Result         map[string]any `json:"result"` // 结构化测试结果，内部含 required test 名
	SignerKeyID    string         `json:"signer_key_id"`
	Signature      string         `json:"signature"` // base64 Ed25519 over SigningBytes(不含 signature 字段)
}

// SigningBytes 构造被签名的规范化字节串。
// 字段固定、顺序固定：{"artifact_digest":...,"evidence_id":...,"result":{...canonical...},
// "tests_passed":...,"version":N}
func SigningBytes(e Envelope) ([]byte, error) {
	return canon.Encode(map[string]any{
		"evidence_id":     e.EvidenceID,
		"version":         e.Version,
		"artifact_digest": e.ArtifactDigest,
		"tests_passed":    e.TestsPassed,
		"result":          e.Result,
	})
}

// Verify 验签 + 基本完整性
func (e Envelope) Verify() error {
	if e.EvidenceID == "" {
		return fmt.Errorf("evidence_id required")
	}
	if e.Version <= 0 {
		return fmt.Errorf("evidence version must be > 0")
	}
	if e.SignerKeyID == "" || e.Signature == "" {
		return fmt.Errorf("evidence signer and signature required")
	}
	msg, err := SigningBytes(e)
	if err != nil {
		return fmt.Errorf("canonicalize evidence: %w", err)
	}
	if err := sig.Verify(e.SignerKeyID, msg, e.Signature); err != nil {
		return err
	}
	return nil
}

// RequiredTestPassed 从 result.tests 中读取指定测试是否通过。
// 约定 result 形如 {"tests":{"unit":true,"integration":true,...}, "ran_at":...}
func RequiredTestPassed(result map[string]any, name string) bool {
	raw, ok := result["tests"]
	if !ok {
		return false
	}
	tests, ok := raw.(map[string]any)
	if !ok {
		return false
	}
	v, ok := tests[name]
	if !ok {
		return false
	}
	pass, ok := v.(bool)
	return ok && pass
}

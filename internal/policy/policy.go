// Package policy 定义晋级策略的不可变版本。
// 晋升请求按 (policy_id, version) 精确钉版；新策略必须以更大的 version 另行登记，
// 历史版本保留可审计，不允许原地修改。
package policy

import (
	"encoding/json"
	"fmt"

	"atomicpromo/internal/crypto/canon"
	"atomicpromo/internal/crypto/sig"
)

// Body 策略内容
type Body struct {
	PolicyID         string   `json:"policy_id"`
	Version          int64    `json:"version"`
	RequiredTests    []string `json:"required_tests"`    // 证据 result.tests 中必须为 true 的测试
	MinSigners       int      `json:"min_signers"`       // 证据签名者数量门槛（本实现中证据单签，必须 <=1 并已验签）
	ApprovalRequired bool     `json:"approval_required"` // 晋升到该环境是否需要额外审批
	// 保留规则（GC 时生效，回退目标也必须仍满足）：
	KeepLastN int `json:"keep_last_n"` // 每个环境至少保留最近 N 个完整历史版本
}

// SignedBody 登记策略时的请求：策略正文 + 权威签名者对 canonical(body) 的 Ed25519 签名
type SignedBody struct {
	Body        Body   `json:"body"`
	SignerKeyID string `json:"signer_key_id"`
	Signature   string `json:"signature"`
}

func (b Body) Validate() error {
	if b.PolicyID == "" {
		return fmt.Errorf("policy_id required")
	}
	if b.Version <= 0 {
		return fmt.Errorf("policy version must be > 0")
	}
	if b.MinSigners < 0 || b.MinSigners > 1 {
		return fmt.Errorf("min_signers must be 0 or 1 in this implementation")
	}
	if b.KeepLastN < 0 {
		return fmt.Errorf("keep_last_n must be >= 0")
	}
	return nil
}

// Canonical 返回策略正文的规范化字节（版本哈希与签名都基于它）
func (b Body) Canonical() ([]byte, error) {
	return canon.Encode(b)
}

// VersionHash sha256(canonical body)，策略版本内容指纹
func (b Body) VersionHash() (string, error) {
	raw, err := b.Canonical()
	if err != nil {
		return "", err
	}
	return sig.HashBytes(raw), nil
}

// Verify 校验策略正文签名
func (s SignedBody) Verify() error {
	if err := s.Body.Validate(); err != nil {
		return err
	}
	if s.SignerKeyID == "" || s.Signature == "" {
		return fmt.Errorf("policy signer and signature required")
	}
	raw, err := s.Body.Canonical()
	if err != nil {
		return err
	}
	return sig.Verify(s.SignerKeyID, raw, s.Signature)
}

// UnmarshalBody 从存储的 JSON 还原 Body
func UnmarshalBody(s string) (Body, error) {
	var b Body
	if err := json.Unmarshal([]byte(s), &b); err != nil {
		return Body{}, err
	}
	return b, b.Validate()
}

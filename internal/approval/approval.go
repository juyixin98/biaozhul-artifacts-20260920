// Package approval 定义人工审批。审批签名钉死完整晋升元组：
// (policy_id, policy_version, env, artifact_digest, evidence_id, evidence_version, expected_gen)。
// expected_gen 使“迟到审批”可被识别：环境代次已前进后，旧审批不能用于新代次。
package approval

import (
	"fmt"

	"atomicpromo/internal/crypto/canon"
	"atomicpromo/internal/crypto/sig"
)

type Decision struct {
	ApprovalID      string `json:"approval_id"`
	PolicyID        string `json:"policy_id"`
	PolicyVersion   int64  `json:"policy_version"`
	Env             string `json:"env"`
	ArtifactDigest  string `json:"artifact_digest"`
	EvidenceID      string `json:"evidence_id"`
	EvidenceVersion int64  `json:"evidence_version"`
	ExpectedGen     int64  `json:"expected_gen"`
	SignerKeyID     string `json:"signer_key_id"`
	Signature       string `json:"signature"` // 对除 signature 外全部字段的 Ed25519 签名
}

func (d Decision) SigningBytes() ([]byte, error) {
	return canon.Encode(map[string]any{
		"approval_id":      d.ApprovalID,
		"policy_id":        d.PolicyID,
		"policy_version":   d.PolicyVersion,
		"env":              d.Env,
		"artifact_digest":  d.ArtifactDigest,
		"evidence_id":      d.EvidenceID,
		"evidence_version": d.EvidenceVersion,
		"expected_gen":     d.ExpectedGen,
		"signer_key_id":    d.SignerKeyID,
	})
}

func (d Decision) Verify() error {
	if d.ApprovalID == "" || d.SignerKeyID == "" || d.Signature == "" {
		return fmt.Errorf("approval id, signer and signature required")
	}
	if d.PolicyVersion <= 0 || d.EvidenceVersion <= 0 {
		return fmt.Errorf("policy/evidence versions required in approval tuple")
	}
	if d.ArtifactDigest == "" || d.EvidenceID == "" || d.Env == "" || d.PolicyID == "" {
		return fmt.Errorf("approval tuple must pin env, digest, evidence and policy")
	}
	msg, err := d.SigningBytes()
	if err != nil {
		return err
	}
	return sig.Verify(d.SignerKeyID, msg, d.Signature)
}

// Tuple 去掉签名与 key id 后的“晋升元组”，用于与晋升请求逐项比对
type Tuple struct {
	ApprovalID      string
	PolicyID        string
	PolicyVersion   int64
	Env             string
	ArtifactDigest  string
	EvidenceID      string
	EvidenceVersion int64
	ExpectedGen     int64
}

func (d Decision) Tuple() Tuple {
	return Tuple{
		ApprovalID:      d.ApprovalID,
		PolicyID:        d.PolicyID,
		PolicyVersion:   d.PolicyVersion,
		Env:             d.Env,
		ArtifactDigest:  d.ArtifactDigest,
		EvidenceID:      d.EvidenceID,
		EvidenceVersion: d.EvidenceVersion,
		ExpectedGen:     d.ExpectedGen,
	}
}

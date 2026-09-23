// Package core 实现凭证发行、撤销与历史验证的领域逻辑。
package core

import (
	"crypto/ed25519"
	"errors"
	"time"
)

// ErrNotFound 表示查询的实体不存在。
var ErrNotFound = errors.New("not found")

// Key 是签发者的一把 ed25519 密钥，带有生效区间 [ValidFrom, ValidTo)。
// ValidTo 为 nil 表示当前有效。轮换只追加新 Key 并封闭旧 Key 的区间。
type Key struct {
	Kid       string
	Issuer    string
	PublicKey ed25519.PublicKey
	SecretKey ed25519.PrivateKey // 仅测试用途：本地签名
	ValidFrom time.Time
	ValidTo   *time.Time
}

// Active 报告 t 是否落在密钥生效区间内。
func (k *Key) Active(t time.Time) bool {
	if t.Before(k.ValidFrom) {
		return false
	}
	if k.ValidTo != nil && !t.Before(*k.ValidTo) {
		return false
	}
	return true
}

// Credential 是一张本地签名凭证，绑定签发者、主体、用途、有效期与内容摘要。
type Credential struct {
	ID            string
	Issuer        string
	Subject       string
	Purpose       string
	NotBefore     time.Time
	NotAfter      time.Time
	ContentDigest string // 内容 sha256 的十六进制编码
	Kid           string
	Signature     []byte
	IssuedAt      time.Time
}

// RevocationEvent 是一条只追加的撤销事件；Seq 即全局快照号。
type RevocationEvent struct {
	Seq          int64
	CredentialID string
	Reason       string
	RecordedAt   time.Time
}

// VerifyRequest 是一次验证请求。At 为零值时表示“现在”。
type VerifyRequest struct {
	CredentialID string
	Purpose      string
	At           time.Time
	Content      *string // 可选：提供则校验内容摘要
}

// 验证失败原因码。
const (
	ReasonSignatureInvalid      = "signature_invalid"
	ReasonKeyInactiveAtIssuance = "key_not_active_at_issuance"
	ReasonPurposeMismatch       = "purpose_mismatch"
	ReasonNotYetValid           = "not_yet_valid"
	ReasonExpired               = "expired"
	ReasonDigestMismatch        = "content_digest_mismatch"
	ReasonRevoked               = "revoked"
)

// 验证结论状态。
const (
	StatusValid   = "valid"
	StatusInvalid = "invalid"
)

// VerifyResult 是一次验证的结论。Snapshot 是得出结论时所依据的全局快照号，
// 与撤销接口返回的快照号属于同一序列。
type VerifyResult struct {
	CredentialID string
	Status       string
	Reasons      []string
	Snapshot     int64
	CheckedAt    time.Time
	CacheHit     bool
}

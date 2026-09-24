package attest

import (
	"crypto/ed25519"
	"fmt"
)

// Verifier 按策略验证构建证明封装。
type Verifier struct {
	policy *Policy
	replay *ReplayCache
}

func NewVerifier(p *Policy) *Verifier {
	return &Verifier{policy: p, replay: NewReplayCache()}
}

// Result 是一次验证的结论。Reason 在 Accepted=false 时给出拒绝原因。
type Result struct {
	Accepted      bool   `json:"accepted"`
	Reason        string `json:"reason,omitempty"`
	AttestationID string `json:"attestationId,omitempty"`
	Builder       string `json:"builder,omitempty"`
	KeyID         string `json:"keyId,omitempty"`
}

func reject(format string, args ...any) Result {
	return Result{Accepted: false, Reason: fmt.Sprintf(format, args...)}
}

// Verify 对请求体（规范 JSON 封装）执行完整验证流水线。
// 顺序：严格解析 → 结构/格式 → 用途 → 密钥窗口 → 签名 → 策略 → 时效 → 重放。
func (v *Verifier) Verify(body []byte) Result {
	raw, err := ParseStrict(body)
	if err != nil {
		return reject("JSON 严格解析失败: %v", err)
	}
	env, err := parseEnvelope(raw)
	if err != nil {
		return reject("封装结构非法: %v", err)
	}
	a := env.Attestation

	// 用途绑定：拒绝跨用途签名。
	if a.Purpose != PurposeV1 {
		return reject("purpose %q 不是预期的 %q（跨用途签名拒绝）", a.Purpose, PurposeV1)
	}

	// 密钥查找与时间窗（轮换：保留生效与撤销时间）。
	key, ok := v.policy.Keys[env.KeyID]
	if !ok {
		return reject("未知 keyId %q", env.KeyID)
	}
	now := v.policy.now()
	if !key.keyUsableAt(now) {
		return reject("密钥 %q 当前不在生效窗口内（已撤销或未生效）", env.KeyID)
	}
	if !key.keyUsableAt(a.Timestamp) {
		return reject("证明时间戳不在密钥 %q 的生效窗口内", env.KeyID)
	}

	// 真实 ed25519 验签：DomainSep || Canonical(attestation)。
	if !ed25519.Verify(key.PublicKey, signMessage(env.AttestationCanonical), env.Sig) {
		return reject("ed25519 签名验证失败")
	}

	// 信任策略：签名有效但策略不符仍拒绝。
	if !v.policy.TrustedBuilders[a.Builder] {
		return reject("builder %q 不在可信列表中", a.Builder)
	}
	if !v.policy.AllowedRepos[a.SourceRepo] {
		return reject("源码仓库 %q 不在允许列表中", a.SourceRepo)
	}

	// 时效：证明不能过旧，也不能来自未来。
	if now.Sub(a.Timestamp) > v.policy.MaxAge {
		return reject("证明已过期（超过最大年龄 %s）", v.policy.MaxAge)
	}
	if a.Timestamp.Sub(now) > v.policy.MaxAge {
		return reject("证明时间戳超出允许的未来偏移")
	}

	// 重放：attestationId 一次性使用。
	if !v.replay.Add(a.AttestationID, a.Timestamp.Add(v.policy.MaxAge), now) {
		return reject("attestationId %q 已被使用（重放拒绝）", a.AttestationID)
	}

	return Result{
		Accepted:      true,
		AttestationID: a.AttestationID,
		Builder:       a.Builder,
		KeyID:         env.KeyID,
	}
}

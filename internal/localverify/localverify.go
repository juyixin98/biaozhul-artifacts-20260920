// Package localverify 实现“本地测试验签器”：真实校验镜像配置摘要与 ed25519 签名，
// 产出由验签器私钥签名的 VerificationResult 信封。
package localverify

import (
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"mirrorsec/internal/cryptox"
	"mirrorsec/internal/model"
)

// Outcome 是验签器对镜像签名的真实判定。
type Outcome string

const (
	OutcomeSigned       Outcome = "SIGNED"        // 签名有效且签名者受信
	OutcomeUnsigned     Outcome = "UNSIGNED"      // 未提供签名
	OutcomeBadSignature Outcome = "BAD_SIGNATURE" // 提供了签名但验签失败
	OutcomeUntrusted    Outcome = "UNTRUSTED_KEY" // 签名有效但签名者不在受信列表
)

// Input 是验签器输入。
type Input struct {
	Image          []byte
	SBOM           []byte // 可空
	ImageSignature []byte // 可空（模拟镜像随附签名）
	TrustedSigners []ed25519.PublicKey
}

// Run 真实执行验签，返回结果载荷（尚未封入信封）。
func Run(in Input, verifierID string, now time.Time) (model.VerificationResult, Outcome, error) {
	if len(in.Image) == 0 {
		return model.VerificationResult{}, "", errors.New("镜像配置为空")
	}
	imageDigest, err := cryptox.CanonicalDigest(in.Image)
	if err != nil {
		return model.VerificationResult{}, "", fmt.Errorf("计算镜像摘要: %w", err)
	}
	sbomDigest := ""
	if len(in.SBOM) > 0 {
		if sbomDigest, err = cryptox.CanonicalDigest(in.SBOM); err != nil {
			return model.VerificationResult{}, "", fmt.Errorf("计算 SBOM 摘要: %w", err)
		}
	}

	result := model.VerificationResult{
		ImageDigest: imageDigest,
		SBOMDigest:  sbomDigest,
		VerifiedAt:  now.UTC().Format(time.RFC3339Nano),
		VerifierID:  verifierID,
	}
	outcome := OutcomeUnsigned

	if len(in.ImageSignature) > 0 {
		var sig model.ImageSignature
		if err := json.Unmarshal(in.ImageSignature, &sig); err != nil {
			return model.VerificationResult{}, "", fmt.Errorf("解析镜像签名: %w", err)
		}
		outcome = classify(in.Image, sig, in.TrustedSigners, &result)
	}

	result.SignatureValid = outcome == OutcomeSigned
	return result, outcome, nil
}

func classify(image []byte, sig model.ImageSignature, trusted []ed25519.PublicKey, result *model.VerificationResult) Outcome {
	if sig.Algorithm != "ed25519" {
		return OutcomeBadSignature
	}
	sigBytes, err := cryptox.B64Decode(sig.Signature)
	if err != nil {
		return OutcomeBadSignature
	}
	// 签名针对镜像 canonical 摘要（与 imageDigest 同一摘要）真实验签。
	canonical, err := cryptox.CanonicalJSONBytes(image)
	if err != nil {
		return OutcomeBadSignature
	}
	digest := cryptox.SHA256Hex(canonical)
	signerTrusted := false
	// 即使签名最终无效，只要声称的 keyId 在受信列表中，就回填 SignedBy，
	// 使下游能区分“受信者的坏签名(BAD_SIGNATURE)”与“完全未签名(UNSIGNED)”。
	for _, pub := range trusted {
		if cryptox.KeyID(pub) != sig.KeyID {
			continue
		}
		result.SignedBy = sig.KeyID
		// 即使签名者受信，若签名绑定的是别的摘要（标签漂移），验签仍失败。
		if sig.ImageDigest != "" && sig.ImageDigest != digest {
			return OutcomeBadSignature
		}
		if ed25519.Verify(pub, canonical, sigBytes) {
			signerTrusted = true
			break
		}
	}
	switch {
	case signerTrusted:
		return OutcomeSigned
	case keyKnown(sig.KeyID, trusted):
		return OutcomeBadSignature // 认识密钥但签名不成立
	default:
		result.SignedBy = sig.KeyID
		return OutcomeUntrusted
	}
}

func keyKnown(id string, trusted []ed25519.PublicKey) bool {
	for _, pub := range trusted {
		if cryptox.KeyID(pub) == id {
			return true
		}
	}
	return false
}

// Seal 用验签器私钥对结果签发信封。
func Seal(result model.VerificationResult, verifierPriv ed25519.PrivateKey) (model.Envelope, error) {
	canonical, err := cryptox.CanonicalJSON(result)
	if err != nil {
		return model.Envelope{}, err
	}
	sig := cryptox.SignRaw(verifierPriv, canonical)
	pub, ok := verifierPriv.Public().(ed25519.PublicKey)
	if !ok {
		return model.Envelope{}, errors.New("验签器私钥类型错误")
	}
	return model.Envelope{
		Payload:   canonical,
		Algorithm: "ed25519",
		KeyID:     cryptox.KeyID(pub),
		Signature: cryptox.B64Encode(sig),
	}, nil
}

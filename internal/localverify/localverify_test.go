package localverify_test

import (
	"crypto/ed25519"
	"encoding/json"
	"testing"
	"time"

	"mirrorsec/internal/cryptox"
	"mirrorsec/internal/localverify"
	"mirrorsec/internal/model"
)

var now = time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC)

// mutateLastBase64Char 把首个非填充 base64 字符换成字母表中的另一个字符，
// 得到仍可解码但必然改变首字节的签名（确保走到“验签失败”，而非解码错误）。
func mutateLastBase64Char(s string) string {
	b := []byte(s)
	const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
	cur := string(b[0])
	for _, c := range alphabet {
		if string(c) != cur {
			b[0] = byte(c)
			break
		}
	}
	return string(b)
}

func sign(canonical []byte, priv ed25519.PrivateKey) []byte {
	return signWithDigest(canonical, priv, cryptox.SHA256Hex(canonical))
}

func signWithDigest(canonical []byte, priv ed25519.PrivateKey, digest string) []byte {
	pub := priv.Public().(ed25519.PublicKey)
	env := model.ImageSignature{
		Algorithm:   "ed25519",
		KeyID:       cryptox.KeyID(pub),
		ImageDigest: digest,
		Signature:   cryptox.B64Encode(cryptox.SignRaw(priv, canonical)),
	}
	b, _ := json.Marshal(env)
	return b
}

func TestVerifierOutcomes(t *testing.T) {
	signerPub, signerPriv, _ := cryptox.GenerateKeyPair()
	_, otherPriv, _ := cryptox.GenerateKeyPair()
	verifierPub, verifierPriv, _ := cryptox.GenerateKeyPair()

	image := []byte(`{"repository":"r","config":{"user":"u"}}`)
	canonical, _ := cryptox.CanonicalJSONBytes(image)
	trusted := []ed25519.PublicKey{signerPub}

	// 无签名 => UNSIGNED
	res, outcome, err := localverify.Run(localverify.Input{Image: image, TrustedSigners: trusted}, "v1", now)
	if err != nil || outcome != localverify.OutcomeUnsigned || res.SignatureValid {
		t.Fatalf("无签名应为 UNSIGNED，got %s %v %v", outcome, res.SignatureValid, err)
	}

	// 受信签名 => SIGNED，且结果信封可被验签器公钥验开
	res, outcome, err = localverify.Run(localverify.Input{
		Image: image, ImageSignature: sign(canonical, signerPriv), TrustedSigners: trusted,
	}, "v1", now)
	if err != nil || outcome != localverify.OutcomeSigned || !res.SignatureValid {
		t.Fatalf("有效签名应为 SIGNED，got %s %v %v", outcome, res.SignatureValid, err)
	}
	if res.ImageDigest != cryptox.SHA256Hex(canonical) {
		t.Fatal("验签结果必须绑定真实镜像摘要")
	}
	env, err := localverify.Seal(res, verifierPriv)
	if err != nil {
		t.Fatal(err)
	}
	sig, _ := cryptox.B64Decode(env.Signature)
	if _, ok, err := cryptox.Verify(verifierPub, json.RawMessage(env.Payload), sig); err != nil || !ok {
		t.Fatal("验签结果信封应可用验签器公钥验开")
	}

	// 签名绑定旧摘要（标签漂移）=> BAD_SIGNATURE
	badDigest := signWithDigest(canonical, signerPriv,
		"sha256:0000000000000000000000000000000000000000000000000000000000000000")
	_, outcome, _ = localverify.Run(localverify.Input{
		Image: image, ImageSignature: badDigest, TrustedSigners: trusted,
	}, "v1", now)
	if outcome != localverify.OutcomeBadSignature {
		t.Fatalf("绑定错误摘要应为 BAD_SIGNATURE，got %s", outcome)
	}

	// 受信密钥、摘要正确，但签名字节损坏 => BAD_SIGNATURE（而不是 UNSIGNED）
	corrupt := sign(canonical, signerPriv)
	var corruptEnv model.ImageSignature
	if err := json.Unmarshal(corrupt, &corruptEnv); err != nil {
		t.Fatal(err)
	}
	corruptEnv.Signature = mutateLastBase64Char(corruptEnv.Signature)
	corrupt, _ = json.Marshal(corruptEnv)
	res, outcome, _ = localverify.Run(localverify.Input{
		Image: image, ImageSignature: corrupt, TrustedSigners: trusted,
	}, "v1", now)
	if outcome != localverify.OutcomeBadSignature {
		t.Fatalf("受信密钥坏签名应 BAD_SIGNATURE，got %s", outcome)
	}
	if res.SignedBy == "" {
		t.Fatal("坏签名仍应回填受信签名者 keyId，以区别于 UNSIGNED")
	}

	// 不受信签名者 => UNTRUSTED_KEY
	_, outcome, _ = localverify.Run(localverify.Input{
		Image: image, ImageSignature: sign(canonical, otherPriv), TrustedSigners: trusted,
	}, "v1", now)
	if outcome != localverify.OutcomeUntrusted {
		t.Fatalf("不受信签名者应 UNTRUSTED_KEY，got %s", outcome)
	}

	// 损坏内容 => 错误（如实上报）
	if _, _, err := localverify.Run(localverify.Input{Image: []byte("not-json")}, "v1", now); err == nil {
		t.Fatal("非法镜像 JSON 必须返回错误")
	}
}

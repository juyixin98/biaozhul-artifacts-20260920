package cryptox_test

import (
	"crypto/ed25519"
	"encoding/json"
	"strings"
	"testing"

	"mirrorsec/internal/cryptox"
)

func TestCanonicalJSONDeterministic(t *testing.T) {
	a := `{"b":1,"a":[3,2,1],"c":{"z":true,"y":false}}`
	b := `{ "c": { "y": false, "z": true }, "a": [3,2,1], "b": 1 }` + "\n"
	ca, err := cryptox.CanonicalJSONBytes([]byte(a))
	if err != nil {
		t.Fatal(err)
	}
	cb, err := cryptox.CanonicalJSONBytes([]byte(b))
	if err != nil {
		t.Fatal(err)
	}
	if string(ca) != string(cb) {
		t.Fatalf("canonical 不一致:\n%s\n%s", ca, cb)
	}
	// HTML 字符不得转义（否则不同实现摘要不同）。
	html, err := cryptox.CanonicalJSON(map[string]any{"u": "a<b>&c"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(html), `\u003c`) {
		t.Fatalf("发生 HTML 转义: %s", html)
	}
	// 摘要稳定性：同内容必须同摘要。
	if cryptox.SHA256Hex(ca) != cryptox.SHA256Hex(cb) {
		t.Fatal("摘要随键序/空白变化")
	}
}

func TestSignVerifyRoundTripAndTamper(t *testing.T) {
	pub, priv, err := cryptox.GenerateKeyPair()
	if err != nil {
		t.Fatal(err)
	}
	doc := map[string]any{"digest": "sha256:abc", "rule": "IMG-RUN-ROOT"}
	sig, canonical, err := cryptox.Sign(priv, doc)
	if err != nil {
		t.Fatal(err)
	}
	// 用不同键序/空白的同一文档验签必须通过。
	reshuffled := json.RawMessage(`{ "rule": "IMG-RUN-ROOT", "digest": "sha256:abc" }`)
	_, ok, err := cryptox.Verify(pub, reshuffled, sig)
	if err != nil || !ok {
		t.Fatalf("canonical 验签失败: ok=%v err=%v", ok, err)
	}
	// 篡改一个字节必须验签失败。
	tampered := append([]byte(nil), canonical...)
	tampered[len(tampered)-1] ^= 0xff
	if ed25519.Verify(pub, tampered, sig) {
		t.Fatal("篡改载荷后签名居然通过")
	}
	// 另一把公钥必须验签失败。
	otherPub, _, err := cryptox.GenerateKeyPair()
	if err != nil {
		t.Fatal(err)
	}
	if ed25519.Verify(otherPub, canonical, sig) {
		t.Fatal("错误公钥验签通过")
	}
}

func TestKeyPEMRoundTrip(t *testing.T) {
	pub, priv, err := cryptox.GenerateKeyPair()
	if err != nil {
		t.Fatal(err)
	}
	privPEM, err := cryptox.MarshalPrivateKey(priv)
	if err != nil {
		t.Fatal(err)
	}
	pubPEM, err := cryptox.MarshalPublicKey(pub)
	if err != nil {
		t.Fatal(err)
	}
	priv2, err := cryptox.ParsePrivateKeyPEM(privPEM)
	if err != nil {
		t.Fatal(err)
	}
	pub2, err := cryptox.ParsePublicKeyPEM(pubPEM)
	if err != nil {
		t.Fatal(err)
	}
	msg := []byte("admission-evidence")
	sig := ed25519.Sign(priv2, msg)
	if !ed25519.Verify(pub2, msg, sig) {
		t.Fatal("PEM 往返后密钥不匹配")
	}
	if cryptox.KeyID(pub) != cryptox.KeyID(pub2) {
		t.Fatal("KeyID 不稳定")
	}
}

package attest

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"strings"
	"testing"
	"time"
)

var (
	testNow     = time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC)
	testDigest  = "sha256:" + strings.Repeat("a", 64)
	testCommit  = strings.Repeat("b", 40)
	testRepo    = "https://github.com/example/project"
	testBuilder = "builder-alice"
)

// testEnv 持有一套测试密钥与策略。
type testEnv struct {
	policy *Policy
	keyID  string
	priv   ed25519.PrivateKey
	verif  *Verifier
}

func newTestEnv(t *testing.T) *testEnv {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	policyJSON := fmt.Sprintf(`{
  "trustedBuilders": [%q],
  "allowedSourceRepos": [%q],
  "maxAttestationAgeSeconds": 600,
  "keys": [
    {"keyId": "key-2026a", "publicKey": %q,
     "validFrom": "2026-01-01T00:00:00Z", "validUntil": "2027-01-01T00:00:00Z"}
  ]
}`, testBuilder, testRepo, base64.StdEncoding.EncodeToString(pub))
	p, err := LoadPolicy([]byte(policyJSON))
	if err != nil {
		t.Fatalf("加载策略失败: %v", err)
	}
	p.now = func() time.Time { return testNow }
	return &testEnv{policy: p, keyID: "key-2026a", priv: priv, verif: NewVerifier(p)}
}

func (e *testEnv) validAttestation() Attestation {
	return Attestation{
		AttestationID:  "att-" + strings.Repeat("1", 16),
		Purpose:        PurposeV1,
		ArtifactDigest: testDigest,
		SourceRepo:     testRepo,
		SourceCommit:   testCommit,
		Builder:        testBuilder,
		BuildParams:    map[string]string{"goos": "linux", "goarch": "amd64"},
		Timestamp:      testNow,
	}
}

func (e *testEnv) sign(t *testing.T, a Attestation) []byte {
	t.Helper()
	env, err := SignEnvelope(a, e.keyID, e.priv)
	if err != nil {
		t.Fatal(err)
	}
	return env
}

func mustReject(t *testing.T, res Result, wantSub string) {
	t.Helper()
	if res.Accepted {
		t.Fatalf("期望拒绝，但被接受")
	}
	if !strings.Contains(res.Reason, wantSub) {
		t.Fatalf("拒绝原因 %q 不含预期子串 %q", res.Reason, wantSub)
	}
}

func TestValidAccepted(t *testing.T) {
	e := newTestEnv(t)
	res := e.verif.Verify(e.sign(t, e.validAttestation()))
	if !res.Accepted {
		t.Fatalf("合法证明被拒绝: %s", res.Reason)
	}
	if res.Builder != testBuilder || res.KeyID != "key-2026a" {
		t.Fatalf("返回字段不符: %+v", res)
	}
}

func TestTamperedDigest(t *testing.T) {
	e := newTestEnv(t)
	env := e.sign(t, e.validAttestation())
	// 改 digest 的一个字符（签名不变，必然验签失败）。
	tampered := strings.Replace(string(env), strings.Repeat("a", 64), strings.Repeat("c", 64), 1)
	res := e.verif.Verify([]byte(tampered))
	mustReject(t, res, "签名验证失败")
}

func TestTamperedDigestRecomputeSig(t *testing.T) {
	// 攻击者改了 digest 后用自己的密钥重签——密钥不在策略中。
	e := newTestEnv(t)
	a := e.validAttestation()
	a.ArtifactDigest = "sha256:" + strings.Repeat("d", 64)
	_, evilPriv, _ := ed25519.GenerateKey(rand.Reader)
	env, err := SignEnvelope(a, e.keyID, evilPriv)
	if err != nil {
		t.Fatal(err)
	}
	res := e.verif.Verify(env)
	mustReject(t, res, "签名验证失败")
}

func TestOldKeyRejected(t *testing.T) {
	e := newTestEnv(t)
	// 把密钥撤销时间改到过去，模拟轮换后的旧密钥。
	entry := e.policy.Keys[e.keyID]
	entry.ValidUntil = testNow.Add(-time.Hour)
	e.policy.Keys[e.keyID] = entry

	res := e.verif.Verify(e.sign(t, e.validAttestation()))
	mustReject(t, res, "不在生效窗口内")
}

func TestKeyNotYetValid(t *testing.T) {
	e := newTestEnv(t)
	entry := e.policy.Keys[e.keyID]
	entry.ValidFrom = testNow.Add(time.Hour)
	e.policy.Keys[e.keyID] = entry

	res := e.verif.Verify(e.sign(t, e.validAttestation()))
	mustReject(t, res, "不在生效窗口内")
}

func TestTimestampOutsideKeyWindow(t *testing.T) {
	e := newTestEnv(t)
	// 密钥当前仍有效，但证明时间戳落在密钥生效之前。
	a := e.validAttestation()
	a.Timestamp = time.Date(2025, 6, 1, 0, 0, 0, 0, time.UTC)
	res := e.verif.Verify(e.sign(t, a))
	mustReject(t, res, "时间戳不在密钥")
}

func TestUnknownKeyID(t *testing.T) {
	e := newTestEnv(t)
	env, err := SignEnvelope(e.validAttestation(), "key-does-not-exist", e.priv)
	if err != nil {
		t.Fatal(err)
	}
	res := e.verif.Verify(env)
	mustReject(t, res, "未知 keyId")
}

func TestExtraFieldInAttestation(t *testing.T) {
	e := newTestEnv(t)
	env := e.sign(t, e.validAttestation())
	// 向 attestation 注入额外字段。
	injected := strings.Replace(string(env),
		`"attestation":{`, `"attestation":{"backdoor":"yes",`, 1)
	res := e.verif.Verify([]byte(injected))
	mustReject(t, res, "未知字段")
}

func TestExtraFieldInEnvelope(t *testing.T) {
	e := newTestEnv(t)
	env := e.sign(t, e.validAttestation())
	injected := strings.Replace(string(env), `{"attestation"`, `{"extra":1,"attestation"`, 1)
	res := e.verif.Verify([]byte(injected))
	mustReject(t, res, "未知字段")
}

func TestMissingField(t *testing.T) {
	e := newTestEnv(t)
	env := e.sign(t, e.validAttestation())
	removed := strings.Replace(string(env), `"builder":"`+testBuilder+`",`, ``, 1)
	res := e.verif.Verify([]byte(removed))
	mustReject(t, res, "缺少必需字段")
}

func TestReplay(t *testing.T) {
	e := newTestEnv(t)
	env := e.sign(t, e.validAttestation())
	if res := e.verif.Verify(env); !res.Accepted {
		t.Fatalf("首次验证应通过: %s", res.Reason)
	}
	res := e.verif.Verify(env)
	mustReject(t, res, "重放")
}

func TestDuplicateJSONKeys(t *testing.T) {
	dup := `{"attestation":{},"attestation":{},"signature":{}}`
	e := newTestEnv(t)
	res := e.verif.Verify([]byte(dup))
	mustReject(t, res, "重复键")
}

func TestNonCanonicalNumbers(t *testing.T) {
	e := newTestEnv(t)
	// 合法 JSON 但非本规范所接受的数值写法 → 必须命中“非规范数值”。
	for _, num := range []string{"1.0", "1e3", "-0", "1.5", "0.5", "1E3"} {
		body := `{"attestation":{"attestationId":` + num + `},"signature":{}}`
		res := e.verif.Verify([]byte(body))
		mustReject(t, res, "非规范数值")
	}
	// 连合法 JSON 都不是的写法 → 必须被拒绝（原因不限）。
	for _, num := range []string{"01", "+1", "1.", ".5", "0x10"} {
		body := `{"attestation":{"attestationId":` + num + `},"signature":{}}`
		if res := e.verif.Verify([]byte(body)); res.Accepted {
			t.Fatalf("数值 %s 应被拒绝", num)
		}
	}
}

func TestCanonicalIntegersAccepted(t *testing.T) {
	for _, num := range []string{"0", "1", "-1", "123456789"} {
		v, err := ParseStrict([]byte(`{"n":` + num + `}`))
		if err != nil {
			t.Fatalf("规范整数 %s 不应被拒绝: %v", num, err)
		}
		canon, err := Canonical(v)
		if err != nil {
			t.Fatal(err)
		}
		if string(canon) != `{"n":`+num+`}` {
			t.Fatalf("规范化结果不符: %s", canon)
		}
	}
}

func TestCrossPurposeSignature(t *testing.T) {
	e := newTestEnv(t)
	// 用同一密钥签名一个其它用途的消息（例如登录挑战）。
	a := e.validAttestation()
	a.Purpose = "login-challenge/v1"
	res := e.verif.Verify(e.sign(t, a))
	mustReject(t, res, "跨用途")
}

func TestUntrustedBuilder(t *testing.T) {
	e := newTestEnv(t)
	a := e.validAttestation()
	a.Builder = "builder-mallory"
	res := e.verif.Verify(e.sign(t, a))
	mustReject(t, res, "不在可信列表")
}

func TestDisallowedRepo(t *testing.T) {
	e := newTestEnv(t)
	a := e.validAttestation()
	a.SourceRepo = "https://github.com/example/other"
	res := e.verif.Verify(e.sign(t, a))
	mustReject(t, res, "不在允许列表")
}

func TestExpiredAttestation(t *testing.T) {
	e := newTestEnv(t)
	a := e.validAttestation()
	a.Timestamp = testNow.Add(-time.Hour) // 超出 600s 最大年龄
	res := e.verif.Verify(e.sign(t, a))
	mustReject(t, res, "已过期")
}

func TestFutureAttestation(t *testing.T) {
	e := newTestEnv(t)
	a := e.validAttestation()
	a.Timestamp = testNow.Add(time.Hour)
	res := e.verif.Verify(e.sign(t, a))
	mustReject(t, res, "未来")
}

func TestBadFormats(t *testing.T) {
	e := newTestEnv(t)
	cases := []struct {
		name string
		mut  func(*Attestation)
		want string
	}{
		{"digest 大写", func(a *Attestation) { a.ArtifactDigest = "sha256:" + strings.Repeat("A", 64) }, "artifactDigest"},
		{"digest 非 sha256", func(a *Attestation) { a.ArtifactDigest = "md5:" + strings.Repeat("a", 32) }, "artifactDigest"},
		{"commit 太短", func(a *Attestation) { a.SourceCommit = "abc" }, "sourceCommit"},
		{"repo 非 https", func(a *Attestation) { a.SourceRepo = "http://github.com/example/project" }, "https"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a := e.validAttestation()
			tc.mut(&a)
			res := e.verif.Verify(e.sign(t, a))
			mustReject(t, res, tc.want)
		})
	}
}

func TestNonUTCTimestamp(t *testing.T) {
	e := newTestEnv(t)
	// SignEnvelope 总是输出 UTC，因此手工把 Z 改成 +08:00 偏移。
	env := string(e.sign(t, e.validAttestation()))
	shifted := strings.Replace(env, "T12:00:00Z", "T20:00:00+08:00", 1)
	if shifted == env {
		t.Fatal("替换未生效")
	}
	res := e.verif.Verify([]byte(shifted))
	mustReject(t, res, "UTC")
}

func TestTrailingData(t *testing.T) {
	e := newTestEnv(t)
	env := e.sign(t, e.validAttestation())
	res := e.verif.Verify(append(env, []byte(` {"x":1}`)...))
	mustReject(t, res, "尾随数据")
}

func TestCanonicalDeterministic(t *testing.T) {
	// 同一逻辑内容、不同键序，规范化后字节一致。
	v1, err := ParseStrict([]byte(`{"b":2,"a":1}`))
	if err != nil {
		t.Fatal(err)
	}
	v2, err := ParseStrict([]byte(`{"a":1,"b":2}`))
	if err != nil {
		t.Fatal(err)
	}
	c1, _ := Canonical(v1)
	c2, _ := Canonical(v2)
	if string(c1) != string(c2) || string(c1) != `{"a":1,"b":2}` {
		t.Fatalf("规范化不确定: %s vs %s", c1, c2)
	}
}

func TestKeyRotationBothKeysValid(t *testing.T) {
	e := newTestEnv(t)
	// 加入第二把密钥（轮换中的新密钥）。
	pub2, priv2, _ := ed25519.GenerateKey(rand.Reader)
	e.policy.Keys["key-2026b"] = KeyEntry{
		KeyID:      "key-2026b",
		PublicKey:  pub2,
		ValidFrom:  testNow.Add(-time.Hour),
		ValidUntil: testNow.Add(24 * time.Hour),
	}
	env, err := SignEnvelope(e.validAttestation(), "key-2026b", priv2)
	if err != nil {
		t.Fatal(err)
	}
	if res := e.verif.Verify(env); !res.Accepted {
		t.Fatalf("轮换新密钥应被接受: %s", res.Reason)
	}
}

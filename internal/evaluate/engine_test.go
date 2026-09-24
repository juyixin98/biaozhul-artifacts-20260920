package evaluate_test

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"

	"mirrorsec/internal/cryptox"
	"mirrorsec/internal/evaluate"
	"mirrorsec/internal/localverify"
	"mirrorsec/internal/model"
	"mirrorsec/internal/policy"
)

type fixedClock struct{ t time.Time }

func (c fixedClock) Now() time.Time { return c.t }

type fixture struct {
	t               *testing.T
	signerPub       ed25519.PublicKey
	signerPriv      ed25519.PrivateKey
	otherSignerPub  ed25519.PublicKey
	otherSignerPriv ed25519.PrivateKey
	verifierPub     ed25519.PublicKey
	verifierPriv    ed25519.PrivateKey
	exemptPub       ed25519.PublicKey
	exemptPriv      ed25519.PrivateKey
	allowlist       []model.AllowlistEntry
	now             time.Time
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	fx := &fixture{now: time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC)}
	fx.signerPub, fx.signerPriv, _ = cryptox.GenerateKeyPair()
	fx.otherSignerPub, fx.otherSignerPriv, _ = cryptox.GenerateKeyPair()
	fx.verifierPub, fx.verifierPriv, _ = cryptox.GenerateKeyPair()
	fx.exemptPub, fx.exemptPriv, _ = cryptox.GenerateKeyPair()
	// 允许列表：直接用嵌入策略的默认列表（其条目真实存在）。
	allow, err := policy.DefaultAllowlist()
	if err != nil {
		// 允许列表占位尚未生成时，退化为运行时构造的一份列表。
		allow = nil
	}
	if len(allow) > 0 && stringsHasPrefix(allow[0].Digest, "sha256:PLACEHOLDER") {
		allow = nil
	}
	if allow == nil {
		// 用测试内基础镜像真实计算的摘要。
		base := testImage("registry.local/base/distroless", "1.0", "nonroot", boolPtr(false), nil, nil)
		d, _ := cryptox.CanonicalDigest(base)
		allow = []model.AllowlistEntry{{Repository: "registry.local/base/distroless", Digest: d}}
	}
	fx.allowlist = allow
	return fx
}

func stringsHasPrefix(s, p string) bool {
	return len(s) >= len(p) && s[:len(p)] == p
}

func (fx *fixture) engineAt(now time.Time) *evaluate.Engine {
	eng, err := evaluate.NewEngine(context.Background(), evaluate.Trust{
		Verifier:           pemPub(fx.verifierPub),
		ExemptionAuthority: pemPub(fx.exemptPub),
		TrustedSignerPEMs:  [][]byte{pemPub(fx.signerPub)},
	}, fx.allowlist, fixedClock{t: now})
	if err != nil {
		fx.t.Fatalf("NewEngine: %v", err)
	}
	return eng
}

func (fx *fixture) engine() *evaluate.Engine { return fx.engineAt(fx.now) }

func pemPub(k ed25519.PublicKey) []byte {
	b, _ := cryptox.MarshalPublicKey(k)
	return b
}

// testImage 构造镜像配置 JSON。user/priv 传 nil 表示“字段缺失”。
func testImage(repo, tag, user string, priv *bool, caps []string, base *model.BaseImageRef) []byte {
	cfg := map[string]any{"capAdd": caps}
	if user != "" {
		cfg["user"] = user
	}
	if priv != nil {
		cfg["privileged"] = *priv
	}
	m := map[string]any{"repository": repo, "tag": tag, "config": cfg}
	if base != nil {
		m["baseImage"] = base
	}
	b, _ := json.Marshal(m)
	return b
}

func (fx *fixture) goodImage() ([]byte, *model.BaseImageRef) {
	base := &model.BaseImageRef{Repository: fx.allowlist[0].Repository, Digest: fx.allowlist[0].Digest}
	return testImage("registry.local/app/payments", "v2", "appuser:10001",
		boolPtr(false), []string{"NET_BIND_SERVICE"}, base), base
}

func (fx *fixture) sbom() []byte {
	b, _ := json.Marshal(map[string]any{"bomFormat": "CycloneDX", "component": map[string]any{"name": "payments"}})
	return b
}

func (fx *fixture) signImage(image []byte, priv ed25519.PrivateKey) []byte {
	canonical, _ := cryptox.CanonicalJSONBytes(image)
	pub := priv.Public().(ed25519.PublicKey)
	env := model.ImageSignature{
		Algorithm:   "ed25519",
		KeyID:       cryptox.KeyID(pub),
		ImageDigest: cryptox.SHA256Hex(canonical),
		Signature:   cryptox.B64Encode(cryptox.SignRaw(priv, canonical)),
	}
	b, _ := json.Marshal(env)
	return b
}

func (fx *fixture) verify(image, sbom, imageSig []byte) []byte {
	result, _, err := localverify.Run(localverify.Input{
		Image: image, SBOM: sbom, ImageSignature: imageSig,
		TrustedSigners: []ed25519.PublicKey{fx.signerPub},
	}, "local-verifier-01", fx.now)
	if err != nil {
		fx.t.Fatalf("localverify.Run: %v", err)
	}
	env, err := localverify.Seal(result, fx.verifierPriv)
	if err != nil {
		fx.t.Fatal(err)
	}
	return mustMarshalEnv(env)
}

func (fx *fixture) verifyAt(image, sbom, imageSig []byte, at time.Time) []byte {
	result, _, err := localverify.Run(localverify.Input{
		Image: image, SBOM: sbom, ImageSignature: imageSig,
		TrustedSigners: []ed25519.PublicKey{fx.signerPub},
	}, "local-verifier-01", at)
	if err != nil {
		fx.t.Fatal(err)
	}
	env, err := localverify.Seal(result, fx.verifierPriv)
	if err != nil {
		fx.t.Fatal(err)
	}
	return mustMarshalEnv(env)
}

func mustMarshalEnv(env model.Envelope) []byte {
	b, _ := json.Marshal(env)
	return b
}

func (fx *fixture) grant(digest, ruleID string, expiresAt time.Time, key ed25519.PrivateKey, id string) []byte {
	g := model.ExemptionGrant{
		ID: id, ImageDigest: digest, RuleID: ruleID,
		ExpiresAt: expiresAt.Format(time.RFC3339Nano), CreatedAt: fx.now.Format(time.RFC3339Nano),
	}
	canonical, _ := cryptox.CanonicalJSON(g)
	pub := key.Public().(ed25519.PublicKey)
	env := model.Envelope{
		Payload:   canonical,
		Algorithm: "ed25519",
		KeyID:     cryptox.KeyID(pub),
		Signature: cryptox.B64Encode(cryptox.SignRaw(key, canonical)),
	}
	return mustMarshalEnv(env)
}

func boolPtr(b bool) *bool { return &b }

func eval(t *testing.T, eng *evaluate.Engine, req model.AdmissionRequest) *model.Report {
	t.Helper()
	rep, err := eng.Evaluate(context.Background(), req)
	if err != nil {
		t.Fatalf("Evaluate: %v", err)
	}
	return rep
}

func evalErr(t *testing.T, eng *evaluate.Engine, req model.AdmissionRequest) error {
	t.Helper()
	_, err := eng.Evaluate(context.Background(), req)
	return err
}

func finding(rep *model.Report, ruleID string) model.Finding {
	for _, f := range rep.Findings {
		if f.RuleID == ruleID {
			return f
		}
	}
	return model.Finding{RuleID: ruleID, Status: "<ABSENT>"}
}

// ---------- 判定矩阵 ----------

func TestCompliantImageAllow(t *testing.T) {
	fx := newFixture(t)
	image, _ := fx.goodImage()
	sbom := fx.sbom()
	sig := fx.signImage(image, fx.signerPriv)
	ver := fx.verify(image, sbom, sig)
	rep := eval(t, fx.engine(), model.AdmissionRequest{
		Image: image, SBOM: sbom, Verification: ver,
	})
	if rep.Decision != model.StatusAllow {
		t.Fatalf("want ALLOW, got %s: %+v", rep.Decision, rep.Findings)
	}
	if rep.PolicyVersion != policy.Version || rep.PolicyHash == "" {
		t.Fatal("报告未冻结策略版本/哈希")
	}
}

func TestMissingVerificationIsUnknownNotAllow(t *testing.T) {
	fx := newFixture(t)
	image, _ := fx.goodImage()
	rep := eval(t, fx.engine(), model.AdmissionRequest{Image: image, SBOM: fx.sbom()})
	if rep.Decision != model.StatusUnknown {
		t.Fatalf("缺验签结果必须 UNKNOWN，got %s", rep.Decision)
	}
	if f := finding(rep, model.RuleSigned); f.Status != model.StatusUnknown {
		t.Fatalf("签名规则应 UNKNOWN，got %s", f.Status)
	}
}

func TestMissingImageRejected(t *testing.T) {
	fx := newFixture(t)
	if err := evalErr(t, fx.engine(), model.AdmissionRequest{}); err == nil {
		t.Fatal("缺少 image 必须返回错误（fail-closed）")
	}
}

func TestRootVariantsDenied(t *testing.T) {
	fx := newFixture(t)
	for _, user := range []string{"root", "0", "0:0", "root:1234", " Root "} {
		image := testImage("r", "t", user, boolPtr(false), nil, nil)
		rep := eval(t, fx.engine(), model.AdmissionRequest{Image: image})
		if f := finding(rep, model.RuleRoot); f.Status != model.StatusDeny {
			t.Fatalf("user=%q 应 DENY，got %s (%s)", user, f.Status, f.Reason)
		}
	}
	// 显式空 user（运行时默认 root）=> DENY，与“字段缺失 => UNKNOWN”严格区分。
	image := []byte(`{"repository":"r","tag":"t","config":{"user":"","privileged":false,"capAdd":[]}}`)
	rep := eval(t, fx.engine(), model.AdmissionRequest{Image: image})
	if f := finding(rep, model.RuleRoot); f.Status != model.StatusDeny {
		t.Fatalf("空 user 应 DENY，got %s", f.Status)
	}
	// 1000:0 的 uid 非 0，不应判 root。
	image = testImage("r", "t", "1000:0", boolPtr(false), nil, nil)
	rep = eval(t, fx.engine(), model.AdmissionRequest{Image: image})
	if f := finding(rep, model.RuleRoot); f.Status != model.StatusAllow {
		t.Fatalf("user=1000:0 不应判 root，got %s", f.Status)
	}
}

func TestRootFieldMissingIsUnknown(t *testing.T) {
	fx := newFixture(t)
	// 手工构造彻底缺 user 的配置（缺字段与显式空串必须区分）。
	image := []byte(`{"repository":"r","tag":"t","config":{"privileged":false,"capAdd":[]}}`)
	rep := eval(t, fx.engine(), model.AdmissionRequest{Image: image})
	if f := finding(rep, model.RuleRoot); f.Status != model.StatusUnknown {
		t.Fatalf("缺 user 应 UNKNOWN，got %s", f.Status)
	}
	if f := finding(rep, model.RulePrivileged); f.Status != model.StatusAllow {
		t.Fatalf("privileged=false 应 ALLOW，got %s", f.Status)
	}
}

func TestPrivilegedAndCapAll(t *testing.T) {
	fx := newFixture(t)
	// privileged 缺失 => UNKNOWN
	image := []byte(`{"repository":"r","config":{"user":"u","capAdd":[]}}`)
	rep := eval(t, fx.engine(), model.AdmissionRequest{Image: image})
	if f := finding(rep, model.RulePrivileged); f.Status != model.StatusUnknown {
		t.Fatalf("缺 privileged 应 UNKNOWN，got %s", f.Status)
	}
	// capAdd=ALL 即使 privileged=false 也 DENY
	image = testImage("r", "t", "u", boolPtr(false), []string{"ALL"}, nil)
	rep = eval(t, fx.engine(), model.AdmissionRequest{Image: image})
	if f := finding(rep, model.RulePrivileged); f.Status != model.StatusDeny {
		t.Fatalf("capAdd ALL 应 DENY，got %s", f.Status)
	}
	// 显式特权 => DENY
	image = testImage("r", "t", "u", boolPtr(true), nil, nil)
	rep = eval(t, fx.engine(), model.AdmissionRequest{Image: image})
	if f := finding(rep, model.RulePrivileged); f.Status != model.StatusDeny {
		t.Fatalf("特权应 DENY，got %s", f.Status)
	}
}

func TestBaseAllowlistDigestExact(t *testing.T) {
	fx := newFixture(t)
	// 同仓库但摘要不同 => DENY（tag 漂移无法用可变 tag 绕过）。
	image := testImage("r", "t", "u", boolPtr(false), nil, &model.BaseImageRef{
		Repository: fx.allowlist[0].Repository, Digest: "sha256:0000000000000000000000000000000000000000000000000000000000000000",
	})
	rep := eval(t, fx.engine(), model.AdmissionRequest{Image: image})
	if f := finding(rep, model.RuleBaseAllowlist); f.Status != model.StatusDeny {
		t.Fatalf("摘要不匹配应 DENY，got %s", f.Status)
	}
	// 只有 tag 没有摘要 => UNKNOWN
	image = testImage("r", "t", "u", boolPtr(false), nil, &model.BaseImageRef{
		Repository: fx.allowlist[0].Repository, Digest: "v1.2.3",
	})
	rep = eval(t, fx.engine(), model.AdmissionRequest{Image: image})
	if f := finding(rep, model.RuleBaseAllowlist); f.Status != model.StatusUnknown {
		t.Fatalf("非摘要引用应 UNKNOWN，got %s", f.Status)
	}
}

func TestTagDriftDigestMismatch(t *testing.T) {
	fx := newFixture(t)
	good, _ := fx.goodImage()
	sbom := fx.sbom()
	goodSig := fx.signImage(good, fx.signerPriv)
	goodVer := fx.verify(good, sbom, goodSig)

	// 新内容沿用同一 tag => 新摘要，但携带的是旧验签结果。
	drift := testImage("registry.local/app/payments", "v2", "root",
		boolPtr(false), []string{"NET_BIND_SERVICE"}, &model.BaseImageRef{
			Repository: fx.allowlist[0].Repository, Digest: fx.allowlist[0].Digest})
	rep := eval(t, fx.engine(), model.AdmissionRequest{
		Image: drift, SBOM: sbom, Verification: goodVer,
	})
	if f := finding(rep, model.RuleDigestBind); f.Status != model.StatusDeny {
		t.Fatalf("标签漂移应 IMG-DIGEST-BIND DENY，got %s (%s)", f.Status, f.Reason)
	}
	if rep.Decision != model.StatusDeny {
		t.Fatalf("漂移必须 DENY，got %s", rep.Decision)
	}
}

func TestUntrustedAndBadSigners(t *testing.T) {
	fx := newFixture(t)
	image, _ := fx.goodImage()
	sbom := fx.sbom()

	// 不受信任的签名者：验签器能验出其密钥 id，但准入侧拒绝。
	sig := fx.signImage(image, fx.otherSignerPriv)
	ver := fx.verify(image, sbom, sig)
	rep := eval(t, fx.engine(), model.AdmissionRequest{
		Image: image, SBOM: sbom, Verification: ver,
	})
	if f := finding(rep, model.RuleSigned); f.Status != model.StatusDeny {
		t.Fatalf("不受信签名者应 DENY，got %s (%s)", f.Status, f.Reason)
	}

	// 篡改验签结果信封 => BAD_VERIFICATION_ENVELOPE。
	var env model.Envelope
	if err := json.Unmarshal(ver, &env); err != nil {
		t.Fatal(err)
	}
	env.Signature = env.Signature[:len(env.Signature)-2] + "AA"
	tampered, _ := json.Marshal(env)
	rep = eval(t, fx.engine(), model.AdmissionRequest{
		Image: image, SBOM: sbom, Verification: tampered,
	})
	if f := finding(rep, model.RuleSigned); f.Status != model.StatusDeny ||
		!containsReason(f.Reason, "信封验签失败") {
		t.Fatalf("篡改验签信封应 DENY+信封失败，got %s (%s)", f.Status, f.Reason)
	}
}

func TestTrustedSignerButCorruptSignature(t *testing.T) {
	fx := newFixture(t)
	image, _ := fx.goodImage()
	sbom := fx.sbom()
	sig := fx.signImage(image, fx.signerPriv)
	// 损坏签名字节但保留（受信）keyId 与正确摘要。
	var env model.ImageSignature
	if err := json.Unmarshal(sig, &env); err != nil {
		t.Fatal(err)
	}
	env.Signature = flipFirstBase64Char(env.Signature)
	sig, _ = json.Marshal(env)
	ver := fx.verify(image, sbom, sig)
	rep := eval(t, fx.engine(), model.AdmissionRequest{
		Image: image, SBOM: sbom, Verification: ver,
	})
	if f := finding(rep, model.RuleSigned); f.Status != model.StatusDeny ||
		!containsReason(f.Reason, "签名校验失败") {
		t.Fatalf("受信者的坏签名应 BAD_SIGNATURE DENY，got %s (%s)", f.Status, f.Reason)
	}
}

func TestSBOMMismatch(t *testing.T) {
	fx := newFixture(t)
	image, _ := fx.goodImage()
	sbom := fx.sbom()
	sig := fx.signImage(image, fx.signerPriv)
	ver := fx.verify(image, sbom, sig)
	// 提交另一份 SBOM（内容不同 => 摘要不同）。
	other := []byte(`{"bomFormat":"CycloneDX","component":{"name":"totally-different"}}`)
	rep := eval(t, fx.engine(), model.AdmissionRequest{
		Image: image, SBOM: other, Verification: ver,
	})
	if f := finding(rep, model.RuleDigestBind); f.Status != model.StatusDeny {
		t.Fatalf("SBOM 张冠李戴应 DENY，got %s (%s)", f.Status, f.Reason)
	}
}

// ---------- 豁免：绑定/越界/边界时间 ----------

func TestExemptionBoundaryTime(t *testing.T) {
	fx := newFixture(t)
	image := testImage("registry.local/app/payments", "v2", "0",
		boolPtr(false), nil, &model.BaseImageRef{
			Repository: fx.allowlist[0].Repository, Digest: fx.allowlist[0].Digest})
	digest, _ := cryptox.CanonicalDigest(image)

	cases := []struct {
		name    string
		expires time.Time
		want    string
	}{
		{"到期时刻恰好=now(边界包含)", fx.now, model.StatusExempt},
		{"到期前1纳秒", fx.now.Add(-time.Nanosecond), model.StatusDeny},
		{"到期后1秒", fx.now.Add(time.Second), model.StatusExempt},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			g := fx.grant(digest, model.RuleRoot, tc.expires, fx.exemptPriv, "exm-boundary")
			eng := fx.engine()
			rep := eval(t, eng, model.AdmissionRequest{
				Image:      image,
				Exemptions: []byte("[" + string(g) + "]"),
			})
			f := finding(rep, model.RuleRoot)
			if f.Status != tc.want {
				t.Fatalf("want %s got %s (%s)", tc.want, f.Status, f.Reason)
			}
			if tc.want == model.StatusExempt && f.ExemptionID != "exm-boundary" {
				t.Fatalf("应回带豁免 id，got %q", f.ExemptionID)
			}
			if tc.want == model.StatusDeny {
				bad := finding(rep, model.RuleExemptionBad)
				if bad.Status != model.StatusDeny {
					t.Fatalf("过期豁免必须同时记 IMG-EXEMPTION-INVALID DENY，got %s", bad.Status)
				}
			}
		})
	}
}

func TestExemptionCannotExemptUnknown(t *testing.T) {
	fx := newFixture(t)
	// 镜像缺 user（UNKNOWN），即使有 root 豁免也不能放行。
	image := []byte(`{"repository":"r","config":{"privileged":false,"capAdd":[]}}`)
	digest, _ := cryptox.CanonicalDigest(image)
	g := fx.grant(digest, model.RuleRoot, fx.now.Add(time.Hour), fx.exemptPriv, "exm-unknown")
	rep := eval(t, fx.engine(), model.AdmissionRequest{
		Image:      image,
		Exemptions: []byte("[" + string(g) + "]"),
	})
	if f := finding(rep, model.RuleRoot); f.Status != model.StatusUnknown {
		t.Fatalf("UNKNOWN 不可被豁免，got %s", f.Status)
	}
	if rep.Decision != model.StatusUnknown {
		t.Fatalf("缺证据豁免后仍应 UNKNOWN，got %s", rep.Decision)
	}
}

func TestExemptionOverreachRules(t *testing.T) {
	fx := newFixture(t)
	image, _ := fx.goodImage()
	digest, _ := cryptox.CanonicalDigest(image)
	for _, bad := range []string{model.RuleSigned, model.RuleSBOM, model.RuleDigestBind, model.RuleExemptionBad} {
		g := fx.grant(digest, bad, fx.now.Add(time.Hour), fx.exemptPriv, "exm-"+bad)
		rep := eval(t, fx.engine(), model.AdmissionRequest{
			Image:      image,
			Exemptions: []byte("[" + string(g) + "]"),
		})
		if f := finding(rep, model.RuleExemptionBad); f.Status != model.StatusDeny ||
			!containsReason(f.Reason, "不可豁免") {
			t.Fatalf("规则 %s 的豁免必须被判越界 DENY，got %s (%s)", bad, f.Status, f.Reason)
		}
	}
}

func TestExemptionWrongDigestRejected(t *testing.T) {
	fx := newFixture(t)
	// 豁免绑定到另一个摘要：既不能豁免，还要记录“挪用”。
	g := fx.grant("sha256:deadbeef", model.RuleRoot, fx.now.Add(time.Hour), fx.exemptPriv, "exm-other")
	rootImg := testImage("registry.local/app/payments", "v2", "0",
		boolPtr(false), nil, &model.BaseImageRef{
			Repository: fx.allowlist[0].Repository, Digest: fx.allowlist[0].Digest})
	rep := eval(t, fx.engine(), model.AdmissionRequest{
		Image:      rootImg,
		Exemptions: []byte("[" + string(g) + "]"),
	})
	if f := finding(rep, model.RuleRoot); f.Status != model.StatusDeny {
		t.Fatalf("跨摘要豁免不得生效，got %s", f.Status)
	}
	bad := finding(rep, model.RuleExemptionBad)
	if bad.Status != model.StatusDeny || !containsReason(bad.Reason, "不符") {
		t.Fatalf("应记录跨镜像挪用 DENY，got %s (%s)", bad.Status, bad.Reason)
	}
}

func TestExemptionWrongSignerRejected(t *testing.T) {
	fx := newFixture(t)
	image, _ := fx.goodImage()
	digest, _ := cryptox.CanonicalDigest(image)
	g := fx.grant(digest, model.RuleRoot, fx.now.Add(time.Hour), fx.otherSignerPriv, "exm-badsigner")
	rep := eval(t, fx.engine(), model.AdmissionRequest{
		Image:      image,
		Exemptions: []byte("[" + string(g) + "]"),
	})
	if f := finding(rep, model.RuleExemptionBad); f.Status != model.StatusDeny ||
		!containsReason(f.Reason, "验签失败") {
		t.Fatalf("非豁免机构签发的豁免必须验签失败 DENY，got %s (%s)", f.Status, f.Reason)
	}
}

func TestMalformedExemptionRecordedNotDropped(t *testing.T) {
	fx := newFixture(t)
	image, _ := fx.goodImage()
	// 损坏的信封不能被静默丢弃。
	garbage := []byte(`{"payload":"%","algorithm":"ed25519","keyId":"x","signature":"%%%"}`)
	rep := eval(t, fx.engine(), model.AdmissionRequest{
		Image:      image,
		Exemptions: append(append([]byte("["), garbage...), ']'),
	})
	if f := finding(rep, model.RuleExemptionBad); f.Status != model.StatusDeny {
		t.Fatalf("损坏豁免书必须记 DENY，got %s", f.Status)
	}
}

func containsReason(s, sub string) bool {
	return strings.Contains(s, sub)
}

// flipFirstBase64Char 确定性地换掉签名首个 base64 字符，保证签名字节被改变
// 但仍可解码（用于制造“受信密钥的坏签名”，而非解码错误）。
func flipFirstBase64Char(s string) string {
	if s == "" {
		return s
	}
	const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
	b := []byte(s)
	if alphabet[0] != b[0] {
		b[0] = alphabet[0]
	} else {
		b[0] = alphabet[1]
	}
	return string(b)
}

// ---------- 引擎构造 fail-closed ----------

func TestEngineRejectsEmptyTrustAndAllowlist(t *testing.T) {
	ctx := context.Background()
	if _, err := evaluate.NewEngine(ctx, evaluate.Trust{}, fxAllowlist(t), nil); err == nil {
		t.Fatal("空信任配置必须拒绝启动")
	}
	trust := evaluate.Trust{
		Verifier: pemPub(mustPub(t)), ExemptionAuthority: pemPub(mustPub(t)),
	}
	if _, err := evaluate.NewEngine(ctx, trust, nil, nil); err == nil {
		t.Fatal("空允许列表必须拒绝启动")
	}
}

func fxAllowlist(t *testing.T) []model.AllowlistEntry {
	fx := newFixture(t)
	return fx.allowlist
}

func mustPub(t *testing.T) ed25519.PublicKey {
	pub, _, err := cryptox.GenerateKeyPair()
	if err != nil {
		t.Fatal(err)
	}
	return pub
}

// 保证测试不依赖示例目录（CI 干净检出也能跑）。
func TestMain(m *testing.M) { os.Exit(m.Run()) }

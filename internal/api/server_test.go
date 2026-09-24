package api_test

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"mirrorsec/internal/api"
	"mirrorsec/internal/cryptox"
	"mirrorsec/internal/evaluate"
	"mirrorsec/internal/localverify"
	"mirrorsec/internal/model"
	"mirrorsec/internal/store"
)

func now() time.Time { return time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC) }

type harness struct {
	signerPub    ed25519.PublicKey
	signerPriv   ed25519.PrivateKey
	verifierPriv ed25519.PrivateKey
	allowlist    []model.AllowlistEntry
	server       *httptest.Server
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	signerPub, signerPriv, _ := cryptox.GenerateKeyPair()
	verifierPub, verifierPriv, _ := cryptox.GenerateKeyPair()
	exemptPub, _, _ := cryptox.GenerateKeyPair()

	base := []byte(`{"repository":"registry.local/base/distroless","tag":"1","config":{"user":"nonroot","privileged":false,"capAdd":[]}}`)
	bd, _ := cryptox.CanonicalDigest(base)
	allow := []model.AllowlistEntry{{Repository: "registry.local/base/distroless", Digest: bd}}

	eng, err := evaluate.NewEngine(context.Background(), evaluate.Trust{
		Verifier:           mustPEM(verifierPub),
		ExemptionAuthority: mustPEM(exemptPub),
		TrustedSignerPEMs:  [][]byte{mustPEM(signerPub)},
	}, allow, evaluate.RealClock{})
	if err != nil {
		t.Fatal(err)
	}
	st, err := store.New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(api.NewServer(eng, st).Router())
	t.Cleanup(srv.Close)
	return &harness{
		signerPub: signerPub, signerPriv: signerPriv, verifierPriv: verifierPriv,
		allowlist: allow, server: srv,
	}
}

func mustPEM(k ed25519.PublicKey) []byte {
	b, _ := cryptox.MarshalPublicKey(k)
	return b
}

func (h *harness) goodImage() []byte {
	return []byte(`{"repository":"registry.local/app/x","tag":"v1","config":{` +
		`"user":"app:1000","privileged":false,"capAdd":[]},` +
		`"baseImage":{"repository":"` + h.allowlist[0].Repository + `","digest":"` + h.allowlist[0].Digest + `"}}`)
}

func (h *harness) sbom() []byte {
	return []byte(`{"bomFormat":"CycloneDX","component":{"name":"x"}}`)
}

func (h *harness) ver(image, sbom []byte) []byte {
	canonical, _ := cryptox.CanonicalJSONBytes(image)
	pub := h.signerPriv.Public().(ed25519.PublicKey)
	sig := model.ImageSignature{
		Algorithm: "ed25519", KeyID: cryptox.KeyID(pub),
		ImageDigest: cryptox.SHA256Hex(canonical),
		Signature:   cryptox.B64Encode(cryptox.SignRaw(h.signerPriv, canonical)),
	}
	sigRaw, _ := json.Marshal(sig)
	res, _, err := localverify.Run(localverify.Input{
		Image: image, SBOM: sbom, ImageSignature: sigRaw,
		TrustedSigners: []ed25519.PublicKey{h.signerPub},
	}, "v1", now())
	if err != nil {
		panic(err)
	}
	env, err := localverify.Seal(res, h.verifierPriv)
	if err != nil {
		panic(err)
	}
	raw, _ := json.Marshal(env)
	return raw
}

// 注意：AdmissionRequest 的字节字段在线上是 base64。
func wire(image, sbom, ver []byte) []byte {
	req := map[string]any{"image": base64.StdEncoding.EncodeToString(image)}
	if sbom != nil {
		req["sbom"] = base64.StdEncoding.EncodeToString(sbom)
	}
	if ver != nil {
		req["verification"] = base64.StdEncoding.EncodeToString(ver)
	}
	b, _ := json.Marshal(req)
	return b
}

func post(t *testing.T, url string, body []byte) (int, map[string]any) {
	t.Helper()
	resp, err := http.Post(url, "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var m map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&m)
	return resp.StatusCode, m
}

func TestE2EAllowAndPolicyFrozen(t *testing.T) {
	h := newHarness(t)
	image, sbom := h.goodImage(), h.sbom()
	status, rep := post(t, h.server.URL+"/v1/admission/evaluate", wire(image, sbom, h.ver(image, sbom)))
	if status != http.StatusOK {
		t.Fatalf("status=%d body=%v", status, rep)
	}
	if rep["decision"] != model.StatusAllow {
		t.Fatalf("want ALLOW got %v", rep["decision"])
	}
	if rep["policyVersion"] != "1.0.0-frozen" || rep["policyHash"] == "" {
		t.Fatal("报告必须回带冻结版本与哈希")
	}
	findings, _ := rep["findings"].([]any)
	if len(findings) != 5 {
		t.Fatalf("应有 5 条逐条理由，got %d", len(findings))
	}

	// /v1/policy 返回同一冻结版本。
	resp, err := http.Get(h.server.URL + "/v1/policy")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var info model.PolicyInfo
	if err := json.NewDecoder(resp.Body).Decode(&info); err != nil {
		t.Fatal(err)
	}
	if info.Version != rep["policyVersion"] || info.Hash != rep["policyHash"] {
		t.Fatal("策略端点与报告中的版本/哈希不一致")
	}
}

func TestE2EMissingEvidenceUnknown(t *testing.T) {
	h := newHarness(t)
	status, rep := post(t, h.server.URL+"/v1/admission/evaluate", wire(h.goodImage(), h.sbom(), nil))
	if status != http.StatusOK || rep["decision"] != model.StatusUnknown {
		t.Fatalf("缺验签结果应 200+UNKNOWN，got %d %v", status, rep["decision"])
	}
}

func TestE2EBadRequest422(t *testing.T) {
	h := newHarness(t)
	// 空 image（空 base64）应 fail-closed 422。
	status, body := post(t, h.server.URL+"/v1/admission/evaluate", []byte(`{"image":""}`))
	if status != http.StatusUnprocessableEntity {
		t.Fatalf("空 image 应 422，got %d %v", status, body)
	}
	// 非法 JSON 请求体应 400。
	resp, err := http.Post(h.server.URL+"/v1/admission/evaluate", "application/json", bytes.NewReader([]byte("{bad")))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("非法 JSON 应 400，got %d", resp.StatusCode)
	}
}

func TestE2EReportsPersistAndNotOverwritten(t *testing.T) {
	h := newHarness(t)
	// 第一次：缺证据 UNKNOWN。
	_, r1 := post(t, h.server.URL+"/v1/admission/evaluate", wire(h.goodImage(), nil, nil))
	id1, _ := r1["id"].(string)

	// 第二次：证据齐全 ALLOW。必须是新报告，且第一份原样保留。
	image, sbom := h.goodImage(), h.sbom()
	_, r2 := post(t, h.server.URL+"/v1/admission/evaluate", wire(image, sbom, h.ver(image, sbom)))
	id2, _ := r2["id"].(string)
	if id1 == "" || id1 == id2 {
		t.Fatalf("重评估必须产生新 ID：%q vs %q", id1, id2)
	}

	get := func(id string) map[string]any {
		resp, err := http.Get(h.server.URL + "/v1/reports/" + id)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var m map[string]any
		json.NewDecoder(resp.Body).Decode(&m)
		return m
	}
	if get(id1)["decision"] != model.StatusUnknown {
		t.Fatal("旧 UNKNOWN 报告被覆盖")
	}
	if get(id2)["decision"] != model.StatusAllow {
		t.Fatal("新 ALLOW 报告未持久化")
	}

	resp, err := http.Get(h.server.URL + "/v1/reports")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var list map[string]any
	json.NewDecoder(resp.Body).Decode(&list)
	if n, _ := list["count"].(float64); n != 2 {
		t.Fatalf("报告列表应有 2 条，got %v", list["count"])
	}
}

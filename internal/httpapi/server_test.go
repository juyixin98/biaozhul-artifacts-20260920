package httpapi_test

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/json"
	"io"
	"log/slog"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"

	"atomicpromo/internal/approval"
	"atomicpromo/internal/blob"
	"atomicpromo/internal/crypto/sig"
	"atomicpromo/internal/evidence"
	"atomicpromo/internal/httpapi"
	"atomicpromo/internal/policy"
	"atomicpromo/internal/promotion"
	"atomicpromo/internal/receipt"
	"atomicpromo/internal/store"
)

type hfixture struct {
	t       *testing.T
	ts      *httptest.Server
	db      *store.DB
	ci      ed25519.PrivateKey
	ciPub   ed25519.PublicKey
	pol     ed25519.PrivateKey
	appr    ed25519.PrivateKey
	apprPub ed25519.PublicKey
}

var hOnce sync.Once
var hDB *store.DB

func hDSN() string {
	if v := os.Getenv("TEST_DATABASE_URL"); v != "" {
		return v
	}
	return "postgresql://promo:promo_dev_pwd@127.0.0.1:5432/promo_atomic_http?sslmode=disable"
}

func newHFixture(t *testing.T) *hfixture {
	t.Helper()
	ctx := context.Background()
	hOnce.Do(func() {
		db, err := store.Open(ctx, hDSN())
		if err != nil {
			return
		}
		if err := db.Migrate(ctx, "../../migrations"); err != nil {
			db.Close()
			return
		}
		hDB = db
	})
	if hDB == nil {
		t.Skipf("test DB unavailable (%s); run scripts/db-prepare.sh or set TEST_DATABASE_URL", hDSN())
	}
	for _, tab := range []string{"gc_runs", "env_history", "env_blobs", "env_pointers",
		"promotion_attempts", "approvals", "test_evidence", "policies", "tags", "artifacts"} {
		if _, err := hDB.Pool.Exec(ctx, "TRUNCATE TABLE "+tab+" RESTART IDENTITY CASCADE"); err != nil {
			t.Fatal(err)
		}
	}
	root := t.TempDir()
	bs, err := blob.New(root)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := receipt.LoadOrCreateSigner(root + "/keys")
	if err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	svc := promotion.New(hDB, bs, signer, log)
	srv := httpapi.New(svc, log)
	ts := httptest.NewServer(srv.Router())
	t.Cleanup(ts.Close)

	f := &hfixture{t: t, ts: ts, db: hDB}
	f.ciPub, f.ci = mustKey()
	_, f.pol = mustKey()
	f.apprPub, f.appr = mustKey()
	return f
}

func mustKey() (ed25519.PublicKey, ed25519.PrivateKey) {
	pub, priv, err := sig.GenerateKey()
	if err != nil {
		panic(err)
	}
	return pub, priv
}

func (f *hfixture) post(path string, body any) (int, map[string]any) {
	f.t.Helper()
	b, _ := json.Marshal(body)
	resp, err := http.Post(f.ts.URL+path, "application/json", bytes.NewReader(b))
	if err != nil {
		f.t.Fatal(err)
	}
	defer resp.Body.Close()
	return decode(f.t, resp)
}

func (f *hfixture) postRaw(path, raw string, headers map[string]string) (int, map[string]any) {
	f.t.Helper()
	req, _ := http.NewRequest(http.MethodPost, f.ts.URL+path, strings.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		f.t.Fatal(err)
	}
	defer resp.Body.Close()
	return decode(f.t, resp)
}

func decode(t *testing.T, resp *http.Response) (int, map[string]any) {
	t.Helper()
	raw, _ := io.ReadAll(resp.Body)
	var m map[string]any
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &m); err != nil {
			t.Fatalf("decode %s: %v", string(raw), err)
		}
	}
	return resp.StatusCode, m
}

func (f *hfixture) upload(content string) string {
	f.t.Helper()
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	part, err := mw.CreateFormFile("content", "x.bin")
	if err != nil {
		f.t.Fatal(err)
	}
	part.Write([]byte(content))
	mw.Close()
	resp, err := http.Post(f.ts.URL+"/v1/artifacts", mw.FormDataContentType(), &buf)
	if err != nil {
		f.t.Fatal(err)
	}
	defer resp.Body.Close()
	code, m := decode(f.t, resp)
	if code != http.StatusCreated {
		f.t.Fatalf("upload %d %v", code, m)
	}
	return m["digest"].(string)
}

func (f *hfixture) evidence(id string, ver int64, digest string, tests map[string]any) {
	e := evidence.Envelope{
		EvidenceID: id, Version: ver, ArtifactDigest: digest, TestsPassed: true,
		Result: map[string]any{"tests": tests}, SignerKeyID: sig.PublicKeyHex(f.ciPub),
	}
	msg, _ := evidence.SigningBytes(e)
	e.Signature = sig.Sign(f.ci, msg)
	if code, m := f.post("/v1/evidence", e); code != http.StatusCreated {
		f.t.Fatalf("evidence %d %v", code, m)
	}
}

func (f *hfixture) policy(ver int64, required []string, approval bool, keep int) {
	body := policy.Body{PolicyID: "p", Version: ver, RequiredTests: required,
		MinSigners: 1, ApprovalRequired: approval, KeepLastN: keep}
	raw, _ := body.Canonical()
	sb := policy.SignedBody{Body: body, SignerKeyID: sig.PublicKeyHex(f.pol.Public().(ed25519.PublicKey)),
		Signature: sig.Sign(f.pol, raw)}
	if code, m := f.post("/v1/policies", sb); code != http.StatusCreated {
		f.t.Fatalf("policy %d %v", code, m)
	}
}

func (f *hfixture) approval(id, env, digest string, evid string, evver, gen int64) {
	d := approval.Decision{
		ApprovalID: id, PolicyID: "p", PolicyVersion: 2, Env: env,
		ArtifactDigest: digest, EvidenceID: evid, EvidenceVersion: evver,
		ExpectedGen: gen, SignerKeyID: sig.PublicKeyHex(f.apprPub),
	}
	msg, _ := d.SigningBytes()
	d.Signature = sig.Sign(f.appr, msg)
	if code, m := f.post("/v1/approvals", d); code != http.StatusCreated {
		f.t.Fatalf("approval %d %v", code, m)
	}
}

func TestHTTPRejectFloatingTag(t *testing.T) {
	f := newHFixture(t)
	f.policy(2, []string{"unit"}, false, 2)
	d := f.upload("b1")
	f.evidence("e", 1, d, map[string]any{"unit": true})
	code, m := f.post("/v1/promotions", map[string]any{
		"env": "staging", "digest": "candidate", "evidence_id": "e",
		"evidence_version": 1, "policy_id": "p", "policy_version": 2, "expected_gen": 0,
	})
	// 领域拒绝返回 200 + status=rejected（每次尝试都留证据），原因必须指明不可变引用
	if code != http.StatusOK || m["status"] != "rejected" ||
		!strings.Contains(m["reason"].(string), "immutable") {
		t.Fatalf("tag promotion must be rejected as immutable-ref, got %d %v", code, m)
	}
}

func TestHTTPPromotionReceiptRoundTrip(t *testing.T) {
	f := newHFixture(t)
	f.policy(1, []string{"unit"}, false, 3)
	f.policy(2, []string{"unit"}, true, 2)
	d := f.upload("r1")
	f.evidence("e", 1, d, map[string]any{"unit": true})
	f.approval("a1", "staging", d, "e", 1, 0)
	code, m := f.post("/v1/promotions", map[string]any{
		"env": "staging", "digest": d, "evidence_id": "e", "evidence_version": 1,
		"policy_id": "p", "policy_version": 2, "expected_gen": 0, "approval_id": "a1",
	})
	if code != http.StatusCreated || m["status"] != "committed" {
		t.Fatalf("promote: %d %v", code, m)
	}
	// 收据必须可独立验签
	recMap := m["receipt"].(map[string]any)
	raw, _ := json.Marshal(recMap)
	var sr receipt.Signed
	if err := json.Unmarshal(raw, &sr); err != nil {
		t.Fatal(err)
	}
	if err := receipt.VerifySigned(&sr); err != nil {
		t.Fatalf("receipt: %v", err)
	}
}

func TestHTTPCopyFailureFaultHeader(t *testing.T) {
	// httptest 下故障注入默认受 ALLOW_FAULT_INJECTION 开关控制；
	// 这里直接通过 service+blob context 的方式不便注入，所以仅验证无开关时故障头被忽略，
	// 真实的损坏/崩溃路径由 promotion 包集成测试与 accept.sh 覆盖。
	f := newHFixture(t)
	f.policy(2, []string{"unit"}, false, 2)
	d := f.upload("cf1")
	f.evidence("e", 1, d, map[string]any{"unit": true})
	code, m := f.postRaw("/v1/promotions", `{
	  "env":"staging","digest":"`+d+`","evidence_id":"e","evidence_version":1,
	  "policy_id":"p","policy_version":2,"expected_gen":0}`,
		map[string]string{"X-Test-Fault": "corrupt-copy"})
	// 未开 ALLOW_FAULT_INJECTION：头被忽略，正常晋级成功
	if code != http.StatusCreated || m["status"] != "committed" {
		t.Fatalf("fault header must be ignored without env switch: %d %v", code, m)
	}
}

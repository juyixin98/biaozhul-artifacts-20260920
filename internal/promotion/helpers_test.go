package promotion_test

import (
	"context"
	"crypto/ed25519"
	"io"
	"log/slog"
	"os"
	"strings"
	"sync"
	"testing"

	"atomicpromo/internal/approval"
	"atomicpromo/internal/blob"
	"atomicpromo/internal/crypto/sig"
	"atomicpromo/internal/evidence"
	"atomicpromo/internal/policy"
	"atomicpromo/internal/promotion"
	"atomicpromo/internal/receipt"
	"atomicpromo/internal/store"
)

// fixture 封装一个真实 PostgreSQL 支持的服务实例（测试库 promo_atomic_test）
type fixture struct {
	t      *testing.T
	db     *store.DB
	blobs  *blob.Store
	svc    *promotion.Service
	signer *receipt.Signer

	ciPriv   ed25519.PrivateKey
	ciPub    ed25519.PublicKey
	polPriv  ed25519.PrivateKey
	polPub   ed25519.PublicKey
	apprPriv ed25519.PrivateKey
	apprPub  ed25519.PublicKey
}

func dsn() string {
	if v := os.Getenv("TEST_DATABASE_URL"); v != "" {
		return v
	}
	return "postgresql://promo:promo_dev_pwd@127.0.0.1:5432/promo_atomic_test?sslmode=disable"
}

var (
	sharedOnce sync.Once
	sharedDB   *store.DB
	sharedOK   bool
)

func newFixture(t *testing.T) *fixture {
	t.Helper()
	ctx := context.Background()
	sharedOnce.Do(func() {
		db, err := store.Open(ctx, dsn())
		if err != nil {
			return
		}
		if err := db.Migrate(ctx, "../../migrations"); err != nil {
			db.Close()
			return
		}
		sharedDB = db
		sharedOK = true
	})
	if !sharedOK {
		t.Skipf("PostgreSQL test DB unavailable (%s); run scripts/db-prepare.sh", dsn())
	}

	// 每个测试独立环境名前缀；为简单起见清空业务表（测试库专用）
	cleanTables := []string{
		"gc_runs", "env_history", "env_blobs", "env_pointers",
		"promotion_attempts", "approvals", "test_evidence",
		"policies", "tags", "artifacts",
	}
	for _, tab := range cleanTables {
		if _, err := sharedDB.Pool.Exec(ctx, "TRUNCATE TABLE "+tab+" RESTART IDENTITY CASCADE"); err != nil {
			t.Fatalf("truncate %s: %v", tab, err)
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
	f := &fixture{
		t: t, db: sharedDB, blobs: bs, signer: signer,
		svc: promotion.New(sharedDB, bs, signer, log),
	}
	f.ciPub, f.ciPriv = mustKey()
	f.polPub, f.polPriv = mustKey()
	f.apprPub, f.apprPriv = mustKey()
	return f
}

func mustKey() (ed25519.PublicKey, ed25519.PrivateKey) {
	pub, priv, err := sig.GenerateKey()
	if err != nil {
		panic(err)
	}
	return pub, priv
}

// uploadArtifact 写入内容并登记，返回 digest
func (f *fixture) uploadArtifact(body string) string {
	f.t.Helper()
	d, _, err := f.svc.UploadArtifact(context.Background(), strings.NewReader(body), "")
	if err != nil {
		f.t.Fatalf("upload: %v", err)
	}
	return d
}

// signEvidence 构造并登记一份证据
func (f *fixture) signEvidence(id string, ver int64, digest string, pass bool, tests map[string]any) {
	f.t.Helper()
	e := evidence.Envelope{
		EvidenceID: id, Version: ver, ArtifactDigest: digest, TestsPassed: pass,
		Result:      map[string]any{"tests": tests},
		SignerKeyID: sig.PublicKeyHex(f.ciPub),
	}
	msg, err := evidence.SigningBytes(e)
	if err != nil {
		f.t.Fatal(err)
	}
	e.Signature = sig.Sign(f.ciPriv, msg)
	if err := f.svc.RegisterEvidence(context.Background(), e); err != nil {
		f.t.Fatalf("register evidence: %v", err)
	}
}

func (f *fixture) registerPolicy(ver int64, required []string, approval bool, keep int) {
	f.t.Helper()
	body := policy.Body{
		PolicyID: "p", Version: ver, RequiredTests: required, MinSigners: 1,
		ApprovalRequired: approval, KeepLastN: keep,
	}
	raw, err := body.Canonical()
	if err != nil {
		f.t.Fatal(err)
	}
	sb := policy.SignedBody{Body: body, SignerKeyID: sig.PublicKeyHex(f.polPub),
		Signature: sig.Sign(f.polPriv, raw)}
	if _, err := f.svc.RegisterPolicy(context.Background(), sb); err != nil {
		f.t.Fatalf("register policy: %v", err)
	}
}

// makeApproval 真实签名一个审批并登记
func (f *fixture) makeApproval(id, env, digest string, evid string, evver, gen int64) approval.Decision {
	f.t.Helper()
	d := approval.Decision{
		ApprovalID: id, PolicyID: "p", PolicyVersion: 2, Env: env,
		ArtifactDigest: digest, EvidenceID: evid, EvidenceVersion: evver,
		ExpectedGen: gen, SignerKeyID: sig.PublicKeyHex(f.apprPub),
	}
	msg, err := d.SigningBytes()
	if err != nil {
		f.t.Fatal(err)
	}
	d.Signature = sig.Sign(f.apprPriv, msg)
	if err := f.svc.RegisterApproval(context.Background(), d); err != nil {
		f.t.Fatalf("register approval: %v", err)
	}
	return d
}

func (f *fixture) promoteOK(env string, digest string, evid string, evver, gen int64, approvalID *string) promotion.Outcome {
	f.t.Helper()
	out, err := f.svc.Promote(context.Background(), promotion.PromoteRequest{
		Env: env, Digest: digest, EvidenceID: evid, EvidenceVer: evver,
		PolicyID: "p", PolicyVer: 2, ExpectedGen: gen, ApprovalID: approvalID,
	})
	if err != nil {
		f.t.Fatalf("promote: %v", err)
	}
	if out.Status != "committed" {
		f.t.Fatalf("promote not committed: %+v", out)
	}
	return out
}

func (f *fixture) current(env string) (string, int64) {
	v, err := f.svc.GetEnvironment(context.Background(), env)
	if err != nil {
		f.t.Fatal(err)
	}
	if v.CurrentDigest == nil {
		return "", v.Gen
	}
	return *v.CurrentDigest, v.Gen
}

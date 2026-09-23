package core_test

import (
	"context"
	"crypto/ed25519"
	"fmt"
	"os"
	"sort"
	"sync"
	"testing"
	"time"

	"revcred/internal/core"
	"revcred/internal/store"
	"revcred/migrations"
)

// 集成测试需要真实 PostgreSQL。设置 TEST_DATABASE_URL（或 DATABASE_URL）后运行；
// 未设置时跳过。`make test` 会自动用 docker compose 拉起数据库。
func newService(t *testing.T) (*core.Service, *store.Store) {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		dsn = os.Getenv("DATABASE_URL")
	}
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set; skipping integration test")
	}
	ctx := context.Background()
	st, err := store.New(ctx, dsn)
	if err != nil {
		t.Fatalf("store.New: %v", err)
	}
	if err := st.Migrate(ctx, migrations.FS); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	// 每个测试用例用干净的数据。
	if _, err := st.Pool().Exec(ctx,
		"TRUNCATE revocation_events, credentials, issuer_keys RESTART IDENTITY"); err != nil {
		t.Fatalf("truncate: %v", err)
	}
	t.Cleanup(st.Close)
	return core.NewService(st), st
}

var (
	base = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	in   = core.IssueInput{
		Issuer:    "issuer-synth-1",
		Subject:   "subject-synth-1",
		Purpose:   "age-check",
		NotBefore: base,
		// 窗口覆盖测试运行的真实当前时间，使“现在验证”类用例落在有效期内。
		NotAfter: base.Add(365 * 24 * time.Hour),
		Content:  "synthetic content v1",
	}
)

func issue(t *testing.T, svc *core.Service, input core.IssueInput) *core.Credential {
	t.Helper()
	c, _, err := svc.Issue(context.Background(), input)
	if err != nil {
		t.Fatalf("Issue: %v", err)
	}
	return c
}

func verify(t *testing.T, svc *core.Service, req core.VerifyRequest) core.VerifyResult {
	t.Helper()
	res, err := svc.Verify(context.Background(), req)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	return res
}

func hasReason(res core.VerifyResult, reason string) bool {
	for _, r := range res.Reasons {
		if r == reason {
			return true
		}
	}
	return false
}

func TestIssueAndVerifyValid(t *testing.T) {
	svc, _ := newService(t)
	ctx := context.Background()
	if _, err := svc.CreateKey(ctx, in.Issuer, base.Add(-time.Hour)); err != nil {
		t.Fatalf("CreateKey: %v", err)
	}
	c := issue(t, svc, in)

	res := verify(t, svc, core.VerifyRequest{
		CredentialID: c.ID, Purpose: "age-check", At: base.Add(time.Hour),
	})
	if res.Status != core.StatusValid {
		t.Fatalf("want valid, got %s reasons=%v", res.Status, res.Reasons)
	}
	if res.Snapshot != 0 {
		t.Fatalf("want snapshot 0, got %d", res.Snapshot)
	}
	if res.CacheHit {
		t.Fatalf("first verify must not be a cache hit")
	}
}

func TestExpiryBoundary(t *testing.T) {
	svc, _ := newService(t)
	ctx := context.Background()
	if _, err := svc.CreateKey(ctx, in.Issuer, base.Add(-time.Hour)); err != nil {
		t.Fatalf("CreateKey: %v", err)
	}
	c := issue(t, svc, in)

	cases := []struct {
		name   string
		at     time.Time
		status string
		reason string
	}{
		{"just before not_before", base.Add(-time.Nanosecond), core.StatusInvalid, core.ReasonNotYetValid},
		{"exactly not_before", base, core.StatusValid, ""},
		{"mid validity", base.Add(12 * time.Hour), core.StatusValid, ""},
		{"just before not_after", in.NotAfter.Add(-time.Nanosecond), core.StatusValid, ""},
		{"exactly not_after is expired", in.NotAfter, core.StatusInvalid, core.ReasonExpired},
		{"after not_after", in.NotAfter.Add(time.Hour), core.StatusInvalid, core.ReasonExpired},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			res := verify(t, svc, core.VerifyRequest{
				CredentialID: c.ID, Purpose: "age-check", At: tc.at,
			})
			if res.Status != tc.status {
				t.Fatalf("at %s: want %s, got %s reasons=%v",
					tc.at, tc.status, res.Status, res.Reasons)
			}
			if tc.reason != "" && !hasReason(res, tc.reason) {
				t.Fatalf("at %s: want reason %q in %v", tc.at, tc.reason, res.Reasons)
			}
		})
	}
}

func TestKeyRotationSignatures(t *testing.T) {
	svc, st := newService(t)
	ctx := context.Background()

	// 轮换前：key1 生效（区间从一小时前开始，覆盖真实签发时刻），签发 credA。
	k1, err := svc.CreateKey(ctx, in.Issuer, time.Now().Add(-time.Hour))
	if err != nil {
		t.Fatalf("CreateKey k1: %v", err)
	}
	credA := issue(t, svc, in)

	// 轮换：key2 自此刻生效，key1 区间被封闭到同一时刻。
	rotAt := time.Now().UTC().Truncate(time.Microsecond) // timestamptz 精度为微秒
	k2, err := svc.CreateKey(ctx, in.Issuer, rotAt)
	if err != nil {
		t.Fatalf("CreateKey k2: %v", err)
	}
	credB := issue(t, svc, in)

	if credA.Kid != k1.Kid || credB.Kid != k2.Kid {
		t.Fatalf("credentials bound to wrong keys: A=%s B=%s", credA.Kid, credB.Kid)
	}

	// 旧密钥区间已封闭且保留。
	k1After, err := st.GetKey(ctx, k1.Kid)
	if err != nil {
		t.Fatalf("GetKey k1: %v", err)
	}
	if k1After.ValidTo == nil || !k1After.ValidTo.Equal(rotAt) {
		t.Fatalf("k1 valid_to = %v, want %v", k1After.ValidTo, rotAt)
	}

	// 轮换前后签发的两张凭证都验证通过（各自使用对应区间的密钥）。
	for _, c := range []*core.Credential{credA, credB} {
		res := verify(t, svc, core.VerifyRequest{
			CredentialID: c.ID, Purpose: "age-check", At: base.Add(6 * time.Hour),
		})
		if res.Status != core.StatusValid {
			t.Fatalf("cred %s: want valid, got %s reasons=%v", c.ID, res.Status, res.Reasons)
		}
	}

	// 交叉验签必须失败：credA 的签名不能用 key2 的公钥通过。
	if core.VerifySignature(k2.PublicKey, credA, credA.Signature) {
		t.Fatalf("credA signature must not verify under rotated key k2")
	}
	if !core.VerifySignature(k1.PublicKey, credA, credA.Signature) {
		t.Fatalf("credA signature must verify under original key k1")
	}
}

func TestPurposeMismatch(t *testing.T) {
	svc, _ := newService(t)
	ctx := context.Background()
	if _, err := svc.CreateKey(ctx, in.Issuer, base.Add(-time.Hour)); err != nil {
		t.Fatalf("CreateKey: %v", err)
	}
	c := issue(t, svc, in)

	res := verify(t, svc, core.VerifyRequest{
		CredentialID: c.ID, Purpose: "loan-application", At: base.Add(time.Hour),
	})
	if res.Status != core.StatusInvalid || !hasReason(res, core.ReasonPurposeMismatch) {
		t.Fatalf("want purpose_mismatch, got %s reasons=%v", res.Status, res.Reasons)
	}
}

func TestRevocationSnapshotAndHistory(t *testing.T) {
	svc, _ := newService(t)
	ctx := context.Background()
	if _, err := svc.CreateKey(ctx, in.Issuer, base.Add(-time.Hour)); err != nil {
		t.Fatalf("CreateKey: %v", err)
	}
	c := issue(t, svc, in)

	// 撤销前：有效。
	before := verify(t, svc, core.VerifyRequest{CredentialID: c.ID, Purpose: "age-check"})
	if before.Status != core.StatusValid || before.Snapshot != 0 {
		t.Fatalf("before revoke: want valid@0, got %s@%d", before.Status, before.Snapshot)
	}

	// 撤销：返回快照号。
	ev, err := svc.Revoke(ctx, c.ID, "subject reported compromise")
	if err != nil {
		t.Fatalf("Revoke: %v", err)
	}
	if ev.Seq != 1 {
		t.Fatalf("want seq 1, got %d", ev.Seq)
	}

	// 撤销后：同一快照序列，结论变为 revoked，且快照号与撤销返回的一致。
	after := verify(t, svc, core.VerifyRequest{CredentialID: c.ID, Purpose: "age-check"})
	if after.Status != core.StatusInvalid || !hasReason(after, core.ReasonRevoked) {
		t.Fatalf("after revoke: want revoked, got %s reasons=%v", after.Status, after.Reasons)
	}
	if after.Snapshot != ev.Seq {
		t.Fatalf("verify snapshot %d != revocation snapshot %d", after.Snapshot, ev.Seq)
	}

	// 历史验证：按撤销发生之前的时刻重放，结论仍是 valid ——
	// 当前状态不得改写过去的结论。
	past := verify(t, svc, core.VerifyRequest{
		CredentialID: c.ID, Purpose: "age-check",
		At: ev.RecordedAt.Add(-time.Second),
	})
	if past.Status != core.StatusValid {
		t.Fatalf("historical verify: want valid, got %s reasons=%v", past.Status, past.Reasons)
	}
	// 撤销发生的下一刻：已撤销。
	future := verify(t, svc, core.VerifyRequest{
		CredentialID: c.ID, Purpose: "age-check",
		At: ev.RecordedAt.Add(time.Second),
	})
	if !hasReason(future, core.ReasonRevoked) {
		t.Fatalf("verify at revoke+1s: want revoked, got reasons=%v", future.Reasons)
	}
}

func TestCacheInvalidatedByRevocation(t *testing.T) {
	svc, _ := newService(t)
	ctx := context.Background()
	if _, err := svc.CreateKey(ctx, in.Issuer, base.Add(-time.Hour)); err != nil {
		t.Fatalf("CreateKey: %v", err)
	}
	c := issue(t, svc, in)
	// 固定的验证时刻：晚于撤销发生时间，使撤销重放对其可见。
	at := time.Now().UTC().Add(time.Hour)
	req := core.VerifyRequest{CredentialID: c.ID, Purpose: "age-check", At: at}

	first := verify(t, svc, req)
	if first.CacheHit || first.Status != core.StatusValid {
		t.Fatalf("first: want valid miss, got %s hit=%v", first.Status, first.CacheHit)
	}
	second := verify(t, svc, req)
	if !second.CacheHit || second.Status != core.StatusValid {
		t.Fatalf("second: want valid hit, got %s hit=%v", second.Status, second.CacheHit)
	}

	if _, err := svc.Revoke(ctx, c.ID, "key compromise"); err != nil {
		t.Fatalf("Revoke: %v", err)
	}

	// 撤销推进了快照号，旧的缓存条目不得再命中。
	third := verify(t, svc, req)
	if third.CacheHit {
		t.Fatalf("after revoke: stale cache entry must not be served")
	}
	if third.Status != core.StatusInvalid || !hasReason(third, core.ReasonRevoked) {
		t.Fatalf("after revoke: want revoked, got %s reasons=%v", third.Status, third.Reasons)
	}
}

func TestConcurrentRevocation(t *testing.T) {
	svc, _ := newService(t)
	ctx := context.Background()
	if _, err := svc.CreateKey(ctx, in.Issuer, base.Add(-time.Hour)); err != nil {
		t.Fatalf("CreateKey: %v", err)
	}
	c := issue(t, svc, in)

	const n = 20
	var wg sync.WaitGroup
	seqs := make([]int64, n)
	errs := make([]error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			ev, err := svc.Revoke(ctx, c.ID, fmt.Sprintf("concurrent revoke %d", i))
			if err == nil {
				seqs[i] = ev.Seq
			}
			errs[i] = err
		}(i)
	}
	wg.Wait()
	for i, err := range errs {
		if err != nil {
			t.Fatalf("revoke %d: %v", i, err)
		}
	}

	// 只追加：n 个并发撤销产生 n 条事件，快照号两两不同。
	events, err := svc.ListRevocations(ctx, c.ID)
	if err != nil {
		t.Fatalf("ListRevocations: %v", err)
	}
	if len(events) != n {
		t.Fatalf("want %d events, got %d", n, len(events))
	}
	sorted := append([]int64(nil), seqs...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
	for i := 1; i < n; i++ {
		if sorted[i] == sorted[i-1] {
			t.Fatalf("duplicate snapshot number %d", sorted[i])
		}
	}

	// 最终快照号 == 最大事件 seq；验证结论为 revoked。
	snap, err := svc.Snapshot(ctx)
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if snap != sorted[n-1] {
		t.Fatalf("snapshot %d != max seq %d", snap, sorted[n-1])
	}
	res := verify(t, svc, core.VerifyRequest{CredentialID: c.ID, Purpose: "age-check"})
	if !hasReason(res, core.ReasonRevoked) || res.Snapshot != snap {
		t.Fatalf("want revoked@%d, got %v@%d", snap, res.Reasons, res.Snapshot)
	}
}

func TestContentDigestMismatch(t *testing.T) {
	svc, _ := newService(t)
	ctx := context.Background()
	if _, err := svc.CreateKey(ctx, in.Issuer, base.Add(-time.Hour)); err != nil {
		t.Fatalf("CreateKey: %v", err)
	}
	c := issue(t, svc, in)

	good := "synthetic content v1"
	ok := verify(t, svc, core.VerifyRequest{
		CredentialID: c.ID, Purpose: "age-check", At: base.Add(time.Hour), Content: &good,
	})
	if ok.Status != core.StatusValid {
		t.Fatalf("matching content: want valid, got %s reasons=%v", ok.Status, ok.Reasons)
	}

	bad := "tampered content"
	res := verify(t, svc, core.VerifyRequest{
		CredentialID: c.ID, Purpose: "age-check", At: base.Add(time.Hour), Content: &bad,
	})
	if !hasReason(res, core.ReasonDigestMismatch) {
		t.Fatalf("tampered content: want content_digest_mismatch, got %v", res.Reasons)
	}
}

func TestTamperedSignature(t *testing.T) {
	svc, st := newService(t)
	ctx := context.Background()
	if _, err := svc.CreateKey(ctx, in.Issuer, base.Add(-time.Hour)); err != nil {
		t.Fatalf("CreateKey: %v", err)
	}
	c := issue(t, svc, in)

	// 直接篡改库中签名（模拟数据被改动），验签必须失败。
	tampered := append([]byte(nil), c.Signature...)
	tampered[0] ^= 0xff
	if _, err := st.Pool().Exec(ctx,
		"UPDATE credentials SET signature = $1 WHERE id = $2", tampered, c.ID); err != nil {
		t.Fatalf("tamper: %v", err)
	}
	res := verify(t, svc, core.VerifyRequest{
		CredentialID: c.ID, Purpose: "age-check", At: base.Add(time.Hour),
	})
	if !hasReason(res, core.ReasonSignatureInvalid) {
		t.Fatalf("want signature_invalid, got %v", res.Reasons)
	}

	// 顺带确认真实 ed25519 原语本身拒绝被篡改的签名。
	key, err := st.GetKey(ctx, c.Kid)
	if err != nil {
		t.Fatalf("GetKey: %v", err)
	}
	if ed25519.Verify(key.PublicKey, core.CanonicalPayload(c), tampered) {
		t.Fatalf("ed25519 accepted a tampered signature")
	}
}

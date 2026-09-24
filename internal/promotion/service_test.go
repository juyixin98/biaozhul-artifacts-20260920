package promotion_test

import (
	"context"
	"encoding/json"
	"strings"
	"sync"
	"testing"

	"atomicpromo/internal/blob"
	"atomicpromo/internal/promotion"
	"atomicpromo/internal/receipt"
)

func TestHappyPathAndReceipt(t *testing.T) {
	f := newFixture(t)
	f.registerPolicy(1, []string{"unit", "integration"}, false, 3)
	f.registerPolicy(2, []string{"unit", "integration", "security"}, true, 2)

	d := f.uploadArtifact("build-1")
	f.signEvidence("ev", 1, d, true, map[string]any{"unit": true, "integration": true, "security": true})
	f.makeApproval("ap1", "staging", d, "ev", 1, 0)

	out := f.promoteOK("staging", d, "ev", 1, 0, strPtr("ap1"))
	if out.Receipt == nil {
		t.Fatal("missing signed receipt")
	}
	raw, _ := json.Marshal(out.Receipt)
	var sr receipt.Signed
	if err := json.Unmarshal(raw, &sr); err != nil {
		t.Fatal(err)
	}
	if err := receipt.VerifySigned(&sr); err != nil {
		t.Fatalf("receipt verify: %v", err)
	}
	if sr.Receipt.CopyVerified != d {
		t.Fatalf("receipt copy verified mismatch")
	}
	cur, gen := f.current("staging")
	if cur != d || gen != 1 {
		t.Fatalf("pointer = %s gen %d", cur, gen)
	}
}

func TestTagReferenceRejected(t *testing.T) {
	f := newFixture(t)
	f.registerPolicy(2, []string{"unit"}, false, 2)
	d := f.uploadArtifact("build-tag")
	f.signEvidence("ev", 1, d, true, map[string]any{"unit": true})
	if err := f.svc.MoveTag(context.Background(), "candidate", d); err != nil {
		t.Fatal(err)
	}
	out, err := f.svc.Promote(context.Background(), promotion.PromoteRequest{
		Env: "staging", Digest: "candidate", EvidenceID: "ev", EvidenceVer: 1,
		PolicyID: "p", PolicyVer: 2, ExpectedGen: 0,
	})
	if err == nil && out.Status != "rejected" {
		t.Fatalf("tag reference must be rejected, got outcome %+v", out)
	}
	if err != nil && !strings.Contains(err.Error(), "immutable") {
		t.Fatalf("want immutable-ref rejection, got %v", err)
	}
}

func TestPolicyRejectsMissingRequiredTest(t *testing.T) {
	f := newFixture(t)
	f.registerPolicy(2, []string{"unit", "security"}, false, 2)
	d := f.uploadArtifact("build-2")
	f.signEvidence("ev", 1, d, true, map[string]any{"unit": true}) // security 缺失
	out, err := f.svc.Promote(context.Background(), promotion.PromoteRequest{
		Env: "staging", Digest: d, EvidenceID: "ev", EvidenceVer: 1,
		PolicyID: "p", PolicyVer: 2, ExpectedGen: 0,
	})
	if err != nil {
		t.Fatal(err)
	}
	if out.Status != "rejected" {
		t.Fatalf("want rejected, got %s (%s)", out.Status, out.Reason)
	}
	if _, gen := f.current("staging"); gen != 0 {
		t.Fatalf("pointer must not move, gen=%d", gen)
	}
}

func TestConcurrentPromotionExactlyOneWinner(t *testing.T) {
	f := newFixture(t)
	f.registerPolicy(2, []string{"unit"}, true, 2)
	d1 := f.uploadArtifact("build-x")
	d2 := f.uploadArtifact("build-y")
	f.signEvidence("e1", 1, d1, true, map[string]any{"unit": true})
	f.signEvidence("e2", 1, d2, true, map[string]any{"unit": true})
	f.makeApproval("ax", "race", d1, "e1", 1, 0)
	f.makeApproval("ay", "race", d2, "e2", 1, 0)

	var wg sync.WaitGroup
	res := make(chan string, 2)
	for _, x := range []struct {
		d, ev, ap string
	}{{d1, "e1", "ax"}, {d2, "e2", "ay"}} {
		wg.Add(1)
		go func(d, ev, ap string) {
			defer wg.Done()
			out, err := f.svc.Promote(context.Background(), promotion.PromoteRequest{
				Env: "race", Digest: d, EvidenceID: ev, EvidenceVer: 1,
				PolicyID: "p", PolicyVer: 2, ExpectedGen: 0, ApprovalID: strPtr(ap),
			})
			if err != nil {
				res <- "err:" + err.Error()
				return
			}
			res <- out.Status
		}(x.d, x.ev, x.ap)
	}
	wg.Wait()
	close(res)
	var committed, conflict int
	for s := range res {
		switch s {
		case "committed":
			committed++
		case "conflict":
			conflict++
		default:
			t.Fatalf("unexpected status %s", s)
		}
	}
	if committed != 1 || conflict != 1 {
		t.Fatalf("want 1 committed 1 conflict, got %d/%d", committed, conflict)
	}
	_, gen := f.current("race")
	if gen != 1 {
		t.Fatalf("gen must advance exactly once, got %d", gen)
	}
}

func TestCopyFailureKeepsOldPointerAndEvidence(t *testing.T) {
	f := newFixture(t)
	f.registerPolicy(2, []string{"unit"}, true, 2)
	d1 := f.uploadArtifact("good")
	d2 := f.uploadArtifact("better")
	f.signEvidence("e1", 1, d1, true, map[string]any{"unit": true})
	f.signEvidence("e2", 1, d2, true, map[string]any{"unit": true})
	f.makeApproval("a1", "staging", d1, "e1", 1, 0)
	f.makeApproval("a2", "staging", d2, "e2", 1, 1)
	f.promoteOK("staging", d1, "e1", 1, 0, strPtr("a1"))

	ctx := blob.WithFault(context.Background(), &blob.Fault{CorruptBytes: 4})
	out, err := f.svc.Promote(ctx, promotion.PromoteRequest{
		Env: "staging", Digest: d2, EvidenceID: "e2", EvidenceVer: 1,
		PolicyID: "p", PolicyVer: 2, ExpectedGen: 1, ApprovalID: strPtr("a2"),
	})
	if err != nil {
		t.Fatal(err)
	}
	if out.Status != "copy_failed" {
		t.Fatalf("want copy_failed, got %s (%s)", out.Status, out.Reason)
	}
	cur, gen := f.current("staging")
	if cur != d1 || gen != 1 {
		t.Fatalf("old pointer must survive: %s gen %d", cur, gen)
	}
	att, err := f.svc.GetAttempt(context.Background(), out.AttemptID)
	if err != nil || att.FailureStage == nil || *att.FailureStage != "copy" || att.FinishedAt == nil {
		t.Fatalf("failed attempt evidence incomplete: %+v err=%v", att, err)
	}
	// 审批未被消费：无故障重试同 expected_gen 成功
	f.promoteOK("staging", d2, "e2", 1, 1, strPtr("a2"))
}

func TestLateApprovalRejected(t *testing.T) {
	f := newFixture(t)
	f.registerPolicy(2, []string{"unit"}, true, 2)
	d1 := f.uploadArtifact("p1")
	d2 := f.uploadArtifact("p2")
	f.signEvidence("e1", 1, d1, true, map[string]any{"unit": true})
	f.signEvidence("e2", 1, d2, true, map[string]any{"unit": true})
	f.makeApproval("a1", "staging", d1, "e1", 1, 0)
	f.makeApproval("a2", "staging", d2, "e2", 1, 0) // 同样钉 gen0
	f.promoteOK("staging", d1, "e1", 1, 0, strPtr("a1"))

	// 第二个 gen0 审批在 gen 已为 1 后才送达 → conflict，审批保持 valid
	out, err := f.svc.CompleteApprovedPromotion(context.Background(), "a2", "")
	if err != nil {
		t.Fatal(err)
	}
	if out.Status != "conflict" {
		t.Fatalf("want conflict, got %s: %s", out.Status, out.Reason)
	}
	row, err := f.db.GetApproval(context.Background(), "a2")
	if err != nil || row.Status != "valid" {
		t.Fatalf("late approval must remain valid, got %+v err=%v", row, err)
	}
}

func TestRollbackRules(t *testing.T) {
	f := newFixture(t)
	f.registerPolicy(2, []string{"unit"}, false, 2)
	d1 := f.uploadArtifact("v1")
	d2 := f.uploadArtifact("v2")
	f.signEvidence("e1", 1, d1, true, map[string]any{"unit": true})
	f.signEvidence("e2", 1, d2, true, map[string]any{"unit": true})
	f.promoteOK("staging", d1, "e1", 1, 0, nil)
	f.promoteOK("staging", d2, "e2", 1, 1, nil)

	// 回退到历史 d1 成功
	out, err := f.svc.Rollback(context.Background(), promotion.RollbackRequest{
		Env: "staging", TargetGen: int64Ptr(1), ExpectedGen: 2, PolicyID: "p", PolicyVer: 2,
	})
	if err != nil || out.Status != "committed" {
		t.Fatalf("rollback: %+v err=%v", out, err)
	}
	cur, gen := f.current("staging")
	if cur != d1 || gen != 3 {
		t.Fatalf("after rollback: %s gen %d", cur, gen)
	}

	// 未知 digest 拒绝
	_, err = f.svc.Rollback(context.Background(), promotion.RollbackRequest{
		Env: "staging", Digest: "sha256:0000000000000000000000000000000000000000000000000000000000000000",
		ExpectedGen: 3, PolicyID: "p", PolicyVer: 2,
	})
	if err == nil {
		t.Fatal("rollback to unknown digest must fail")
	}
}

func TestGCAndRollbackToCollectedRejected(t *testing.T) {
	f := newFixture(t)
	f.registerPolicy(2, []string{"unit"}, false, 2)
	d1 := f.uploadArtifact("g1")
	d2 := f.uploadArtifact("g2")
	d3 := f.uploadArtifact("g3")
	for i, d := range []string{d1, d2, d3} {
		ev := "e" + string(rune('1'+i))
		f.signEvidence(ev, 1, d, true, map[string]any{"unit": true})
		f.promoteOK("staging", d, ev, 1, int64(i), nil)
	}
	// 当前 d3(gen3)。keep_last_n=2 保留 d3+d2，d1 被回收
	res, err := f.svc.CollectGarbage(context.Background(), "staging", "p", 2)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, x := range res.RemovedBlobs {
		if x == d1 {
			found = true
		}
	}
	if !found {
		t.Fatalf("d1 should be collected, got removed=%v", res.RemovedBlobs)
	}
	// 回退到 d1 被拒
	if _, err := f.svc.Rollback(context.Background(), promotion.RollbackRequest{
		Env: "staging", Digest: d1, ExpectedGen: 3, PolicyID: "p", PolicyVer: 2,
	}); err == nil {
		t.Fatal("rollback to GC'd blob must be rejected")
	}
	// 回退到保留集 d2 成功
	out, err := f.svc.Rollback(context.Background(), promotion.RollbackRequest{
		Env: "staging", Digest: d2, ExpectedGen: 3, PolicyID: "p", PolicyVer: 2,
	})
	if err != nil || out.Status != "committed" {
		t.Fatalf("rollback to kept blob: %+v err=%v", out, err)
	}
}

func TestIdempotencyKey(t *testing.T) {
	f := newFixture(t)
	f.registerPolicy(2, []string{"unit"}, false, 2)
	d := f.uploadArtifact("idem")
	f.signEvidence("e", 1, d, true, map[string]any{"unit": true})
	key := "fixed-key-1"
	req := promotion.PromoteRequest{
		Env: "staging", Digest: d, EvidenceID: "e", EvidenceVer: 1,
		PolicyID: "p", PolicyVer: 2, ExpectedGen: 0, IdempotencyKey: &key,
	}
	o1, err := f.svc.Promote(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	// 同样的幂等键 + 已过时的 expected_gen，也必须返回同一结果（而非 conflict）
	req.ExpectedGen = 99
	o2, err := f.svc.Promote(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if o1.AttemptID != o2.AttemptID {
		t.Fatalf("idempotency broken: %s != %s", o1.AttemptID, o2.AttemptID)
	}
}

func TestCrashBeforeCommitRecoveryThenRetry(t *testing.T) {
	f := newFixture(t)
	f.registerPolicy(2, []string{"unit"}, true, 2)
	d1 := f.uploadArtifact("k1")
	d2 := f.uploadArtifact("k2")
	f.signEvidence("e1", 1, d1, true, map[string]any{"unit": true})
	f.signEvidence("e2", 1, d2, true, map[string]any{"unit": true})
	f.makeApproval("a1", "staging", d1, "e1", 1, 0)
	f.makeApproval("a2", "staging", d2, "e2", 1, 1)
	f.promoteOK("staging", d1, "e1", 1, 0, strPtr("a1"))

	// 模拟“副本校验完成、提交前崩溃”：CrashHook  panic；panic 发生在 DB 事务之外，
	// 因而效果等同于进程被杀——旧指针保持在 d1，in_progress 尝试与已校验副本留在原地。
	ctx := blob.WithFault(context.Background(), &blob.Fault{CrashBeforePointerCommit: true})
	func() {
		defer func() { _ = recover() }()
		_, _ = f.svc.Promote(ctx, promotion.PromoteRequest{
			Env: "staging", Digest: d2, EvidenceID: "e2", EvidenceVer: 1,
			PolicyID: "p", PolicyVer: 2, ExpectedGen: 1, ApprovalID: strPtr("a2"),
		})
	}()
	cur, gen := f.current("staging")
	if cur != d1 || gen != 1 {
		t.Fatalf("after crash old pointer must remain: %s gen %d", cur, gen)
	}

	// 启动恢复
	rep, err := f.svc.RecoverOnStartup(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(rep.RecoveredAborted) != 1 {
		t.Fatalf("want 1 recovered_aborted, got %+v", rep)
	}
	// 同 expected_gen 重试成功（已校验副本幂等复用；注意 a2 在崩溃路径中未被消费）
	f.promoteOK("staging", d2, "e2", 1, 1, strPtr("a2"))
}

func TestConcurrentRollbackOneConflict(t *testing.T) {
	f := newFixture(t)
	f.registerPolicy(2, []string{"unit"}, false, 5)
	var digests []string
	for i := 0; i < 3; i++ {
		d := f.uploadArtifact("rb" + string(rune('0'+i)))
		ev := "e" + string(rune('0'+i))
		f.signEvidence(ev, 1, d, true, map[string]any{"unit": true})
		digests = append(digests, d)
		f.promoteOK("staging", d, ev, 1, int64(i), nil)
	}
	var wg sync.WaitGroup
	res := make(chan string, 2)
	for _, g := range []int64{1, 2} {
		wg.Add(1)
		go func(g int64) {
			defer wg.Done()
			out, err := f.svc.Rollback(context.Background(), promotion.RollbackRequest{
				Env: "staging", TargetGen: &g, ExpectedGen: 3,
				PolicyID: "p", PolicyVer: 2,
			})
			if err != nil {
				res <- "err"
				return
			}
			res <- out.Status
		}(g)
	}
	wg.Wait()
	close(res)
	committed, conflict := 0, 0
	for s := range res {
		if s == "committed" {
			committed++
		}
		if s == "conflict" {
			conflict++
		}
	}
	if committed != 1 || conflict != 1 {
		t.Fatalf("want 1 committed 1 conflict, got %d/%d", committed, conflict)
	}
}

// ---- 小工具 ----

func strPtr(s string) *string { return &s }
func int64Ptr(v int64) *int64 { return &v }

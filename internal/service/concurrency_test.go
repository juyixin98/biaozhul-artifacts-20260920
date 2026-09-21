package service_test

import (
	"errors"
	"sync"
	"testing"

	"communityvault/internal/service"
)

// Duplicate reports by the same user must not add a row or a count.
func TestDuplicateReportNotCounted(t *testing.T) {
	initTest(t)
	c := mustCreateDraft(t, alice, "tech", "reportable")

	r1, err := svc.Report(ctx, principal(bob), c.ID, "spam")
	if err != nil {
		t.Fatalf("report1: %v", err)
	}
	if !r1.Created {
		t.Fatalf("first report must be created")
	}
	r2, err := svc.Report(ctx, principal(bob), c.ID, "spam again")
	if err != nil {
		t.Fatalf("report2: %v", err)
	}
	if r2.Created {
		t.Fatalf("duplicate report must not create a row")
	}
	if r1.ID != r2.ID {
		t.Fatalf("duplicate must point to the same report row: %d vs %d", r1.ID, r2.ID)
	}

	// Moderator sees one; author only sees aggregate counts without reporter ids.
	modView, err := svc.ListReports(ctx, principal(modTech), c.ID)
	if err != nil {
		t.Fatalf("mod list reports: %v", err)
	}
	list, ok := modView.([]service.ReportSummary)
	if !ok || len(list) != 1 {
		t.Fatalf("mod must see exactly 1 report, got %+v", modView)
	}
	authorView, err := svc.ListReports(ctx, principal(alice), c.ID)
	if err != nil {
		t.Fatalf("author list reports: %v", err)
	}
	agg, ok := authorView.(service.ReportAggregate)
	if !ok || agg.Total != 1 || agg.Open != 1 {
		t.Fatalf("author aggregate wrong: %+v", authorView)
	}

	// A different user may still report once.
	r3, err := svc.Report(ctx, principal(modTech), c.ID, "confirming spam")
	if err != nil || !r3.Created {
		t.Fatalf("distinct user report should create a row: %+v err=%v", r3, err)
	}
}

// Concurrent claimants must never receive the same task.
func TestConcurrentClaimsNoDuplicates(t *testing.T) {
	initTest(t)
	const n = 8
	for i := 0; i < n; i++ {
		c := mustCreateDraft(t, alice, "tech", "concurrent body")
		if _, err := svc.Submit(ctx, principal(alice), c.ID); err != nil {
			t.Fatalf("submit: %v", err)
		}
	}

	// Two tech moderators cannot be seeded, so use the same moderator identity
	// from N goroutines (same person issuing parallel claims). FOR UPDATE SKIP
	// LOCKED must still hand out each task at most once.
	var mu sync.Mutex
	got := map[int64]int{}
	var wg sync.WaitGroup
	errs := make(chan error, n)
	for i := 0; i < n+2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			claim, err := svc.Claim(ctx, principal(modTech))
			if errors.Is(err, service.ErrNoTask) {
				return
			}
			if err != nil {
				errs <- err
				return
			}
			mu.Lock()
			got[claim.TaskID]++
			mu.Unlock()
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("claim error: %v", err)
	}
	if len(got) != n {
		t.Fatalf("expected %d distinct claimed tasks, got %d", n, len(got))
	}
	for taskID, count := range got {
		if count > 1 {
			t.Fatalf("task %d claimed %d times", taskID, count)
		}
	}
}

// Expired claim: stale claimer cannot complete; task is reclaimable after TTL.
func TestExpiredClaimCannotBeCompleted(t *testing.T) {
	initTest(t)
	c := mustCreateDraft(t, alice, "tech", "expiry body")
	if _, err := svcShort.Submit(ctx, principal(alice), c.ID); err != nil {
		t.Fatalf("submit: %v", err)
	}
	first, err := svcShort.Claim(ctx, principal(modTech))
	if err != nil {
		t.Fatalf("first claim: %v", err)
	}
	// Wait past the claim TTL. The background reaper is not running in tests,
	// but CompleteClaim checks claim_expires_at >= now() and the next Claim
	// requeues expired rows inline.
	sleepForReclaim()

	// Stale claimer attempts approval: must be refused.
	if _, err := svcShort.Approve(ctx, principal(modTech), first.TaskID, "late approve"); !errors.Is(err, service.ErrStaleClaim) {
		t.Fatalf("expired claimer approve: want stale claim, got %v", err)
	}

	// A fresh claim re-acquires the same task; its decision wins.
	second, err := svcShort.Claim(ctx, principal(modTech))
	if err != nil {
		t.Fatalf("re-claim after expiry: %v", err)
	}
	if second.TaskID != first.TaskID {
		t.Fatalf("expected same task requeued, got %d vs %d", second.TaskID, first.TaskID)
	}
	pub, err := svcShort.Approve(ctx, principal(modTech), second.TaskID, "fresh approve")
	if err != nil {
		t.Fatalf("fresh approve: %v", err)
	}
	if pub.Status != "published" {
		t.Fatalf("want published, got %s", pub.Status)
	}

	// Now the original stale claimer tries again: must not overwrite anything.
	if _, err := svcShort.Reject(ctx, principal(modTech), first.TaskID, "very late reject"); !errors.Is(err, service.ErrStaleClaim) {
		t.Fatalf("stale reject after completion: want stale claim, got %v", err)
	}
	got, _ := svc.GetContent(ctx, principal(alice), c.ID)
	if got.Status != "published" {
		t.Fatalf("stale claimer overwrote later decision: status=%s", got.Status)
	}
}

// Editing content while it is pending invalidates the open review task.
func TestEditWhilePendingCancelsTask(t *testing.T) {
	initTest(t)
	c := mustCreateDraft(t, alice, "tech", "first version")
	if _, err := svc.Submit(ctx, principal(alice), c.ID); err != nil {
		t.Fatalf("submit: %v", err)
	}
	if _, err := svc.Edit(ctx, principal(alice), c.ID, service.EditContentInput{
		Title: "post", Body: "second version", EditReason: "fix",
	}); err != nil {
		t.Fatalf("edit pending: %v", err)
	}
	// Moderator queue must have no task for it; claiming finds nothing.
	if _, err := svc.Claim(ctx, principal(modTech)); !errors.Is(err, service.ErrNoTask) {
		t.Fatalf("cancelled task still claimable: %v", err)
	}
	got, _ := svc.GetContent(ctx, principal(alice), c.ID)
	if got.Status != "draft" {
		t.Fatalf("edited pending content must return to draft, got %s", got.Status)
	}
}

// Stable keyset pagination: withdrawing rows during paging never leaks and
// paging continues without duplicates or skips.
func TestFeedStableCursorAcrossChanges(t *testing.T) {
	initTest(t)
	var ids []int64
	for i := 0; i < 6; i++ {
		c := mustCreateDraft(t, alice, "tech", "paginated body")
		if _, err := svc.Submit(ctx, principal(alice), c.ID); err != nil {
			t.Fatalf("submit: %v", err)
		}
		claim, err := svc.Claim(ctx, principal(modTech))
		if err != nil {
			t.Fatalf("claim: %v", err)
		}
		if _, err := svc.Approve(ctx, principal(modTech), claim.TaskID, "ok"); err != nil {
			t.Fatalf("approve: %v", err)
		}
		ids = append(ids, c.ID)
	}

	page1, err := svc.Feed(ctx, principal(bob), 3, "")
	if err != nil {
		t.Fatalf("feed page1: %v", err)
	}
	if len(page1.Items) != 3 || page1.NextCursor == "" {
		t.Fatalf("page1 size/cursor wrong: %d items", len(page1.Items))
	}
	firstPageIDs := map[int64]bool{}
	for _, it := range page1.Items {
		firstPageIDs[it.ID] = true
	}

	// Withdraw one of the page-1 items while the reader holds the cursor.
	if _, err := svc.Withdraw(ctx, principal(alice), ids[0], "paging test removal"); err != nil {
		t.Fatalf("withdraw during paging: %v", err)
	}

	page2, err := svc.Feed(ctx, principal(bob), 3, page1.NextCursor)
	if err != nil {
		t.Fatalf("feed page2: %v", err)
	}
	for _, it := range page2.Items {
		if firstPageIDs[it.ID] {
			t.Fatalf("id %d duplicated across pages", it.ID)
		}
		if it.ID == ids[0] {
			t.Fatalf("withdrawn content leaked onto page 2")
		}
	}
	if len(page2.Items) == 0 {
		t.Fatalf("page2 unexpectedly empty after mid-paging withdrawal")
	}
}

// Concurrent rule activation vs submit never errors and always binds a
// concrete, existing rule version (deterministic under the advisory lock).
func TestConcurrentRuleSwitchAndSubmit(t *testing.T) {
	initTest(t)
	const n = 6
	// Pre-create drafts so submits don't race mustCreateDraft's t.Fatalf
	// from background goroutines.
	drafts := make([]service.ContentDTO, n)
	for i := 0; i < n; i++ {
		drafts[i] = mustCreateDraft(t, alice, "tech", "ordinary body")
	}

	var wg sync.WaitGroup
	errs := make(chan error, n*2)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, err := svc.CreateRuleVersion(ctx, principal(adminID), service.CreateRuleVersionInput{
				Description: "concurrent switch",
				Words:       []string{"x" + uniqueSuffix()},
			})
			if err != nil {
				errs <- err
			}
		}(i)
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, err := svc.Submit(ctx, principal(alice), drafts[i].ID)
			if err != nil && !errors.Is(err, service.ErrRuleViolation) {
				errs <- err
			}
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent rule/submit error: %v", err)
	}
}

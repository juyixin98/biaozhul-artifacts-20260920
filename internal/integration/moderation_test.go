package integration

import (
	"sync"
	"testing"

	"communityvault/internal/apperr"
	"communityvault/internal/db"
)

// Repeat reports by the same user on the same content conflict and never add
// a row/counter.
func TestDuplicateReport(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")
	bob := e.user(t, "token-bob")
	c, _, _ := e.cts.Create(e.ctx, alice.ID, 1, "a", "body")

	if _, err := e.mod.Report(e.ctx, bob.ID, c.ID, "spam"); err != nil {
		t.Fatal(err)
	}
	_, err := e.mod.Report(e.ctx, bob.ID, c.ID, "spam again")
	ae, ok := apperr.As(err)
	if !ok || ae.Code != "duplicate_report" {
		t.Fatalf("second report err = %v", err)
	}
	reports, _ := db.New(e.pool).ListReportsForContent(e.ctx, c.ID)
	if len(reports) != 1 {
		t.Fatalf("reports = %d, want 1 (unique key reporter+content)", len(reports))
	}
}

// Authors cannot report their own content.
func TestSelfReportForbidden(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")
	c, _, _ := e.cts.Create(e.ctx, alice.ID, 1, "a", "body")
	if _, err := e.mod.Report(e.ctx, alice.ID, c.ID, "self"); err == nil {
		t.Fatal("self report must fail")
	}
}

// Moderators only claim tasks inside their own categories; a task in 'life'
// is invisible to the tech moderator.
func TestClaimCategoryScoping(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")
	// category 2 = life
	c, _, _ := e.cts.Create(e.ctx, alice.ID, 2, "life post", "body")
	_, _, _, _ = e.cts.Submit(e.ctx, alice.ID, c.ID)

	modTech := e.user(t, "token-mod-tech")
	if _, _, err := e.mod.Claim(e.ctx, modTech, e.catIDs(t, modTech.ID)); err == nil {
		t.Fatal("tech moderator must not claim a life task")
	}
	modLife := e.user(t, "token-mod-life")
	task, _, err := e.mod.Claim(e.ctx, modLife, e.catIDs(t, modLife.ID))
	if err != nil {
		t.Fatalf("life moderator claim: %v", err)
	}
	if task.ContentID != c.ID {
		t.Fatalf("claimed content %d, want %d", task.ContentID, c.ID)
	}
}

// N moderators claiming concurrently over N tasks each get a distinct task:
// no double assignment thanks to FOR UPDATE SKIP LOCKED.
func TestConcurrentClaimsNoDoubleAssign(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")
	mod := e.user(t, "token-mod-tech")
	const n = 8
	var ids []int64
	for i := 0; i < n; i++ {
		c, _, err := e.cts.Create(e.ctx, alice.ID, 1, "p", "body")
		if err != nil {
			t.Fatal(err)
		}
		if _, _, _, err := e.cts.Submit(e.ctx, alice.ID, c.ID); err != nil {
			t.Fatal(err)
		}
		ids = append(ids, c.ID)
	}

	var mu sync.Mutex
	got := map[int64]bool{}
	var wg sync.WaitGroup
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			task, _, err := e.mod.Claim(e.ctx, mod, e.catIDs(t, mod.ID))
			if err != nil {
				errs <- err
				return
			}
			mu.Lock()
			got[task.ContentID] = true
			mu.Unlock()
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("claim: %v", err)
	}
	if len(got) != n {
		t.Fatalf("distinct claims = %d, want %d (double assignment)", len(got), n)
	}
}

// After the claim expires and the task is recycled, the original claimant's
// late decision is rejected with stale_claim and cannot overwrite the later
// moderator's resolution.
func TestStaleClaimCannotOverwrite(t *testing.T) {
	e := setup(t) // default 1h TTL; expiry is forced deterministically below
	alice := e.user(t, "token-alice")
	c, _, _ := e.cts.Create(e.ctx, alice.ID, 1, "a", "clean body")
	_, _, _, _ = e.cts.Submit(e.ctx, alice.ID, c.ID)

	modTech := e.user(t, "token-mod-tech")
	admin := e.user(t, "token-admin")

	task1, claim1, err := e.mod.Claim(e.ctx, modTech, e.catIDs(t, modTech.ID))
	if err != nil {
		t.Fatal(err)
	}
	// Force the first claim into the past (simulating TTL elapse).
	if _, err := e.pool.Exec(e.ctx,
		"UPDATE review_claims SET expires_at = now() - INTERVAL '1 second' WHERE task_id = $1",
		task1.ID); err != nil {
		t.Fatal(err)
	}
	_ = claim1

	// Recycling (scheduler path) reopens the expired task.
	n, err := e.mod.Recycle(e.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("recycled = %d, want 1", n)
	}

	// The original (late) moderator tries to approve: must fail stale.
	if _, err := e.mod.Approve(e.ctx, modTech, task1.ID, e.catIDs(t, modTech.ID)); err == nil {
		t.Fatal("late approval on expired claim must fail")
	} else if ae, ok := apperr.As(err); !ok || ae.Code != "stale_claim" {
		t.Fatalf("late approve err = %v, want stale_claim", err)
	}

	// Someone else claims the recycled task with a fresh live claim and
	// resolves it.
	task2, _, err := e.mod.Claim(e.ctx, admin, nil)
	if err != nil {
		t.Fatalf("admin reclaim: %v", err)
	}
	if task2.ID != task1.ID {
		t.Fatalf("reclaimed task id %d != %d", task2.ID, task1.ID)
	}
	if _, err := e.mod.Approve(e.ctx, admin, task2.ID, nil); err != nil {
		t.Fatalf("admin approve fresh claim: %v", err)
	}

	// Late moderator retries after the resolution: still stale_claim, and the
	// published outcome is unchanged.
	_, lateErr := e.mod.Approve(e.ctx, modTech, task1.ID, e.catIDs(t, modTech.ID))
	if ae, ok := apperr.As(lateErr); !ok || ae.Code != "stale_claim" {
		t.Fatalf("post-resolution late approve err = %v, want stale_claim", lateErr)
	}

	got, _ := db.New(e.pool).GetContent(e.ctx, c.ID)
	if got.Status != "published" || *got.PublishedRevision != task2.RevisionID {
		t.Fatalf("final state = %+v, want published at the claimed revision", got)
	}
}

// If an author withdraws while a moderator holds a claim, the orphaned claim
// is removed with the task; a rollback -> resubmit -> re-claim cycle must
// work (regression for claims PK collision on reopened tasks).
func TestWithdrawWhileClaimedThenResubmit(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")
	modTech := e.user(t, "token-mod-tech")

	c, _, _ := e.cts.Create(e.ctx, alice.ID, 1, "a", "clean body")
	if _, _, _, err := e.cts.Submit(e.ctx, alice.ID, c.ID); err != nil {
		t.Fatal(err)
	}
	task1, _, err := e.mod.Claim(e.ctx, modTech, e.catIDs(t, modTech.ID))
	if err != nil {
		t.Fatal(err)
	}
	// Author withdraws out from under the open claim.
	if _, err := e.cts.Withdraw(e.ctx, alice, c.ID, "changed my mind", nil); err != nil {
		t.Fatal(err)
	}
	// The stale claim holder can no longer resolve it.
	if _, err := e.mod.Approve(e.ctx, modTech, task1.ID, e.catIDs(t, modTech.ID)); err == nil {
		t.Fatal("approval after cancellation must fail")
	}
	// Author rolls back (new revision), resubmits, and the task is
	// re-claimable and completable.
	if _, _, err := e.cts.Rollback(e.ctx, alice.ID, c.ID, 1, "rewrite"); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := e.cts.Submit(e.ctx, alice.ID, c.ID); err != nil {
		t.Fatalf("resubmit: %v", err)
	}
	task2, _, err := e.mod.Claim(e.ctx, modTech, e.catIDs(t, modTech.ID))
	if err != nil {
		t.Fatalf("reclaim after withdraw/resubmit: %v", err)
	}
	if _, err := e.mod.Approve(e.ctx, modTech, task2.ID, e.catIDs(t, modTech.ID)); err != nil {
		t.Fatalf("approve reopened task: %v", err)
	}
	got, _ := db.New(e.pool).GetContent(e.ctx, c.ID)
	if got.Status != "published" {
		t.Fatalf("status = %s, want published", got.Status)
	}
}

package integration

import (
	"sync"
	"testing"

	"communityvault/internal/db"
)

// A new rule version can be staged inactive and activated atomically;
// activation never leaves the system without an active version.
func TestRuleVersioningLifecycle(t *testing.T) {
	e := setup(t)
	admin := e.user(t, "token-admin")
	_ = admin

	rv, err := e.rs.CreateVersion(e.ctx, "stricter", []string{"newword"})
	if err != nil {
		t.Fatal(err)
	}
	if rv.Version != 2 || rv.Active {
		t.Fatalf("new version = %+v", rv)
	}
	// v1 still active while v2 is staged.
	active, words, err := e.rs.Active(e.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if active.Version != 1 || len(words) != 3 {
		t.Fatalf("active during staging = v%d words=%v", active.Version, words)
	}
	if _, err := e.rs.Activate(e.ctx, rv.ID); err != nil {
		t.Fatal(err)
	}
	active, words, _ = e.rs.Active(e.ctx)
	if active.Version != 2 || len(words) != 1 || words[0] != "newword" {
		t.Fatalf("active after activate = v%d words=%v", active.Version, words)
	}
}

// Content submitted under v1 but approved after v2 flags its body is
// auto-rejected: the recorded review is bound to v2, and the old v1-era
// submit cannot push violating text to published.
func TestRuleSwitchDuringReview(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")
	// Body is clean under v1; v2 will ban "reviewed-later".
	c, _, _ := e.cts.Create(e.ctx, alice.ID, 1, "t", "this will be reviewed-later")
	if _, _, _, err := e.cts.Submit(e.ctx, alice.ID, c.ID); err != nil {
		t.Fatalf("submit under v1: %v", err)
	}
	v2, err := e.rs.CreateVersion(e.ctx, "ban reviewed-later", []string{"reviewed-later"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := e.rs.Activate(e.ctx, v2.ID); err != nil {
		t.Fatal(err)
	}
	mod := e.user(t, "token-mod-tech")
	task, _, _ := e.mod.Claim(e.ctx, mod, e.catIDs(t, mod.ID))
	review, err := e.mod.Approve(e.ctx, mod, task.ID, e.catIDs(t, mod.ID))
	if err != nil {
		t.Fatalf("approve call: %v", err)
	}
	if review.Decision != "rejected" {
		t.Fatalf("decision = %s, want rejected", review.Decision)
	}
	if review.RuleVersionID != v2.ID {
		t.Fatalf("review bound to rule %d, want %d", review.RuleVersionID, v2.ID)
	}
	got, _ := db.New(e.pool).GetContent(e.ctx, c.ID)
	if got.Status != "draft" {
		t.Fatalf("status = %s, want draft", got.Status)
	}
}

// Approve racing rule activation must produce a deterministic, internally
// consistent result: the review is bound to exactly one rule version and the
// content status agrees with that version (published only if the body is
// clean under the recorded version).
func TestApproveRacingRuleSwitch(t *testing.T) {
	for iter := 0; iter < 12; iter++ {
		e := setup(t)
		alice := e.user(t, "token-alice")
		mod := e.user(t, "token-mod-tech")
		c, _, _ := e.cts.Create(e.ctx, alice.ID, 1, "race", "racing body here")
		if _, _, _, err := e.cts.Submit(e.ctx, alice.ID, c.ID); err != nil {
			t.Fatal(err)
		}
		task, _, err := e.mod.Claim(e.ctx, mod, e.catIDs(t, mod.ID))
		if err != nil {
			t.Fatal(err)
		}

		v2, err := e.rs.CreateVersion(e.ctx, "ban racing", []string{"racing"})
		if err != nil {
			t.Fatal(err)
		}

		start := make(chan struct{})
		var wg sync.WaitGroup
		wg.Add(2)
		var approveErr error
		var review db.Review
		go func() {
			defer wg.Done()
			<-start
			review, approveErr = e.mod.Approve(e.ctx, mod, task.ID, e.catIDs(t, mod.ID))
		}()
		var activateErr error
		go func() {
			defer wg.Done()
			<-start
			_, activateErr = e.rs.Activate(e.ctx, v2.ID)
		}()
		close(start)
		wg.Wait()
		if activateErr != nil {
			t.Fatalf("activate: %v", activateErr)
		}
		if approveErr != nil {
			t.Fatalf("approve: %v", approveErr)
		}

		got, _ := db.New(e.pool).GetContent(e.ctx, c.ID)
		rv, _ := db.New(e.pool).GetRuleVersion(e.ctx, review.RuleVersionID)
		switch {
		case review.Decision == "approved" && rv.Version == 1 && got.Status == "published":
			// approve acquired the advisory lock first: published under v1.
		case review.Decision == "rejected" && rv.Version == 2 && got.Status == "draft":
			// activation acquired the lock first: rejected under v2.
		default:
			t.Fatalf("iter %d inconsistent: decision=%s rule=v%d status=%s",
				iter, review.Decision, rv.Version, got.Status)
		}
	}
}

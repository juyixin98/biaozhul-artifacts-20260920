package integration

import (
	"strings"
	"testing"

	"communityvault/internal/apperr"
	"communityvault/internal/db"
)

// Every edit creates a new immutable revision; old approvals never authorize a
// new body because pending/published content cannot be edited.
func TestRevisionPerEdit(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")

	c, rev1, err := e.cts.Create(e.ctx, alice.ID, 1, "hello", "first body")
	if err != nil {
		t.Fatal(err)
	}
	if rev1.RevisionNo != 1 {
		t.Fatalf("revision no = %d, want 1", rev1.RevisionNo)
	}
	rev2, err := e.cts.Edit(e.ctx, alice.ID, c.ID, "second body", "typo")
	if err != nil {
		t.Fatal(err)
	}
	if rev2.RevisionNo != 2 || rev2.Origin != "edit" {
		t.Fatalf("rev2 = %+v", rev2)
	}
	// Revision 1 is still on disk, unchanged.
	got1, _ := db.New(e.pool).GetRevisionByNo(e.ctx, db.GetRevisionByNoParams{ContentID: c.ID, RevisionNo: 1})
	if got1.Body != "first body" {
		t.Fatalf("history rewritten: %q", got1.Body)
	}

	// Submit + approve publishes rev2.
	if _, _, _, err := e.cts.Submit(e.ctx, alice.ID, c.ID); err != nil {
		t.Fatal(err)
	}
	mod := e.user(t, "token-mod-tech")
	task, _, err := e.mod.Claim(e.ctx, mod, e.catIDs(t, mod.ID))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := e.mod.Approve(e.ctx, mod, task.ID, e.catIDs(t, mod.ID)); err != nil {
		t.Fatal(err)
	}
	pub, _ := db.New(e.pool).GetContent(e.ctx, c.ID)
	if pub.Status != "published" || *pub.PublishedRevision != rev2.ID {
		t.Fatalf("publish pointers wrong: %+v", pub)
	}

	// Editing published content is rejected: the old approval cannot cover a
	// new body.
	if _, err := e.cts.Edit(e.ctx, alice.ID, c.ID, "third body", ""); err == nil {
		t.Fatal("edit of published content must fail")
	}

	// Withdraw, rollback to rev1 creates rev3 and must be re-reviewed.
	if _, err := e.cts.Withdraw(e.ctx, alice, c.ID, "author withdrawal", nil); err != nil {
		t.Fatal(err)
	}
	rev3, cb, err := e.cts.Rollback(e.ctx, alice.ID, c.ID, 1, "restore first body")
	if err != nil {
		t.Fatal(err)
	}
	if rev3.Origin != "rollback" || rev3.SourceRevisionID == nil || *rev3.SourceRevisionID != rev1.ID {
		t.Fatalf("rollback metadata wrong: %+v", rev3)
	}
	if rev3.Body != "first body" {
		t.Fatalf("rollback body = %q", rev3.Body)
	}
	if cb.Status != "draft" {
		t.Fatalf("after rollback status = %s, want draft", cb.Status)
	}
	// Published revision still points at rev2: history untouched.
	pub2, _ := db.New(e.pool).GetContent(e.ctx, c.ID)
	if *pub2.PublishedRevision != rev2.ID {
		t.Fatal("published_revision must not move on rollback")
	}
}

// A rejection returns the content to draft and keeps the reason; the author
// must revise and resubmit, producing a fresh review against the same rules.
func TestRejectKeepsReasonAndRequiresResubmit(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")
	c, _, err := e.cts.Create(e.ctx, alice.ID, 1, "t", "clean body")
	if err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := e.cts.Submit(e.ctx, alice.ID, c.ID); err != nil {
		t.Fatal(err)
	}
	mod := e.user(t, "token-mod-tech")
	task, _, err := e.mod.Claim(e.ctx, mod, e.catIDs(t, mod.ID))
	if err != nil {
		t.Fatal(err)
	}
	rev, err := e.mod.Reject(e.ctx, mod, task.ID, e.catIDs(t, mod.ID), "needs sources")
	if err != nil {
		t.Fatal(err)
	}
	if rev.Decision != "rejected" || rev.Reason != "needs sources" {
		t.Fatalf("review = %+v", rev)
	}
	got, _ := db.New(e.pool).GetContent(e.ctx, c.ID)
	if got.Status != "draft" {
		t.Fatalf("status = %s, want draft", got.Status)
	}
}

// Reject without a reason is a validation error (all takedowns keep reasons).
func TestRejectRequiresReason(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")
	c, _, _ := e.cts.Create(e.ctx, alice.ID, 1, "t", "body")
	_, _, _, _ = e.cts.Submit(e.ctx, alice.ID, c.ID)
	mod := e.user(t, "token-mod-tech")
	task, _, _ := e.mod.Claim(e.ctx, mod, e.catIDs(t, mod.ID))
	if _, err := e.mod.Reject(e.ctx, mod, task.ID, e.catIDs(t, mod.ID), "  "); err == nil {
		t.Fatal("blank reason must fail")
	}
}

// Only the author may edit; nobody may edit someone else's draft.
func TestAuthorOnlyEdit(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")
	bob := e.user(t, "token-bob")
	c, _, _ := e.cts.Create(e.ctx, alice.ID, 1, "a", "alice body")
	if _, err := e.cts.Edit(e.ctx, bob.ID, c.ID, "hacked", ""); err == nil {
		t.Fatal("non-author edit must fail")
	}
}

// Submitting body that contains an active sensitive word is refused and the
// match is recorded as evidence.
func TestSensitiveWordsOnSubmit(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")
	c, _, _ := e.cts.Create(e.ctx, alice.ID, 1, "a", "this mentions evilco heavily")
	_, matched, _, err := e.cts.Submit(e.ctx, alice.ID, c.ID)
	if err == nil {
		t.Fatal("submit with sensitive word must fail")
	}
	if ae, ok := apperr.As(err); !ok || ae.Code != "sensitive_words" {
		t.Fatalf("err = %v", err)
	}
	if len(matched) != 1 || matched[0] != "evilco" {
		t.Fatalf("matched = %v", matched)
	}
	events, _ := db.New(e.pool).ListEvents(e.ctx, c.ID)
	var found bool
	for _, ev := range events {
		if ev.Event == "rule_checked" && strings.Contains(ev.Reason, "evilco") {
			found = true
		}
	}
	if !found {
		t.Fatal("rule_checked evidence not recorded")
	}
}

// Rollback requires a reason and an existing revision number.
func TestRollbackValidation(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")
	c, _, _ := e.cts.Create(e.ctx, alice.ID, 1, "a", "b1")
	if _, _, err := e.cts.Rollback(e.ctx, alice.ID, c.ID, 1, " "); err == nil {
		t.Fatal("rollback without reason must fail")
	}
	if _, _, err := e.cts.Rollback(e.ctx, alice.ID, c.ID, 99, "r"); err == nil {
		t.Fatal("rollback to missing revision must fail")
	}
}

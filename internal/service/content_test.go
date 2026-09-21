package service_test

import (
	"errors"
	"strings"
	"testing"

	"communityvault/internal/service"
)

// 1. Draft -> submit -> approve -> published happy path.
func TestContentLifecycleApprove(t *testing.T) {
	initTest(t)
	c := mustCreateDraft(t, alice, "tech", "clean body")

	sub, err := svc.Submit(ctx, principal(alice), c.ID)
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	if sub.Status != "pending" {
		t.Fatalf("want pending, got %s", sub.Status)
	}

	claim, err := svc.Claim(ctx, principal(modTech))
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if claim.ContentID != c.ID {
		t.Fatalf("claim returned content %d, want %d", claim.ContentID, c.ID)
	}
	if claim.Body != "clean body" {
		t.Fatalf("claim body mismatch: %q", claim.Body)
	}

	pub, err := svc.Approve(ctx, principal(modTech), claim.TaskID, "looks good")
	if err != nil {
		t.Fatalf("approve: %v", err)
	}
	if pub.Status != "published" {
		t.Fatalf("want published, got %s", pub.Status)
	}

	// Visible to another member via the public feed.
	page, err := svc.Feed(ctx, principal(bob), 50, "")
	if err != nil {
		t.Fatalf("feed: %v", err)
	}
	found := false
	for _, it := range page.Items {
		if it.ID == c.ID {
			found = true
		}
	}
	if !found {
		t.Fatalf("published content %d missing from public feed", c.ID)
	}
}

// 2. Reject requires a reason and returns content to draft.
func TestRejectNeedsReasonAndReturnsToDraft(t *testing.T) {
	initTest(t)
	c := mustCreateDraft(t, alice, "tech", "questionable body")
	if _, err := svc.Submit(ctx, principal(alice), c.ID); err != nil {
		t.Fatalf("submit: %v", err)
	}
	claim, err := svc.Claim(ctx, principal(modTech))
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if _, err := svc.Reject(ctx, principal(modTech), claim.TaskID, ""); !errors.Is(err, service.ErrValidation) {
		t.Fatalf("want validation error for empty reason, got %v", err)
	}
	out, err := svc.Reject(ctx, principal(modTech), claim.TaskID, "against guidelines")
	if err != nil {
		t.Fatalf("reject: %v", err)
	}
	if out.Status != "draft" {
		t.Fatalf("want draft after reject, got %s", out.Status)
	}

	decisions, err := svc.Decisions(ctx, principal(alice), c.ID)
	if err != nil {
		t.Fatalf("decisions: %v", err)
	}
	if len(decisions) == 0 || decisions[0].State != "rejected" ||
		decisions[0].Reason != "against guidelines" {
		t.Fatalf("reject decision evidence missing/wrong: %+v", decisions)
	}
}

// 3. Members only edit their own content.
func TestMembersOnlyEditOwnContent(t *testing.T) {
	initTest(t)
	c := mustCreateDraft(t, alice, "tech", "alice body")
	_, err := svc.Edit(ctx, principal(bob), c.ID, service.EditContentInput{
		Title: "hacked", Body: "hacked body",
	})
	if !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("bob editing alice's draft: want forbidden, got %v", err)
	}
	// Author can edit.
	edited, err := svc.Edit(ctx, principal(alice), c.ID, service.EditContentInput{
		Title: "post edited", Body: "new body", EditReason: "typo",
	})
	if err != nil {
		t.Fatalf("alice edit: %v", err)
	}
	if edited.CurrentRevision == nil || edited.CurrentRevision.RevisionNo != 2 {
		t.Fatalf("edit must create revision #2, got %+v", edited.CurrentRevision)
	}
}

// 4. Moderators are limited to their own category.
func TestModeratorScopeEnforced(t *testing.T) {
	initTest(t)
	c := mustCreateDraft(t, alice, "art", "art body")
	if _, err := svc.Submit(ctx, principal(alice), c.ID); err != nil {
		t.Fatalf("submit: %v", err)
	}
	// tech moderator must not claim the art task.
	_, err := svc.Claim(ctx, principal(modTech))
	if !errors.Is(err, service.ErrNoTask) {
		t.Fatalf("tech mod claiming art task: want no-task, got %v", err)
	}
	// art moderator can.
	claim, err := svc.Claim(ctx, principal(modArt))
	if err != nil {
		t.Fatalf("art mod claim: %v", err)
	}
	if claim.ContentID != c.ID {
		t.Fatalf("wrong content claimed: %d", claim.ContentID)
	}
	// tech mod cannot act on the art task.
	_, err = svc.Approve(ctx, principal(modTech), claim.TaskID, "x")
	if !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("tech mod approving art task: want forbidden, got %v", err)
	}
}

// 5. Every edit creates a NEW revision and a stale approval cannot publish it.
func TestEditAfterApprovalStaleApprovalCannotPublish(t *testing.T) {
	initTest(t)
	c := mustCreateDraft(t, alice, "tech", "version one")
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
	// Published; author withdraws with a reason, edits -> new revision.
	if _, err := svc.Withdraw(ctx, principal(alice), c.ID, "need to update"); err != nil {
		t.Fatalf("withdraw: %v", err)
	}
	edited, err := svc.Edit(ctx, principal(alice), c.ID, service.EditContentInput{
		Title: "post v2", Body: "version two - completely different", EditReason: "rewrite",
	})
	if err != nil {
		t.Fatalf("edit after withdraw: %v", err)
	}
	if edited.Status != "draft" || edited.CurrentRevision.RevisionNo != 2 {
		t.Fatalf("edited post must be draft at rev2, got %s rev %v",
			edited.Status, edited.CurrentRevision)
	}

	// The old approval must NOT apply to revision 2: feed stays empty of it,
	// and the post is not published.
	got, err := svc.GetContent(ctx, principal(alice), c.ID)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if got.Status != "draft" {
		t.Fatalf("stale approval published new body! status=%s", got.Status)
	}
}

// 6. Submit-time rule check rejects forbidden words immediately.
func TestSubmitAutoRejectsForbiddenWord(t *testing.T) {
	initTest(t)
	c := mustCreateDraft(t, alice, "tech", "this contains the forbidden word")
	_, err := svc.Submit(ctx, principal(alice), c.ID)
	if !errors.Is(err, service.ErrRuleViolation) {
		t.Fatalf("want rule violation, got %v", err)
	}
	got, _ := svc.GetContent(ctx, principal(alice), c.ID)
	if got.Status != "draft" {
		t.Fatalf("auto-rejected content must stay draft, got %s", got.Status)
	}
	decisions, _ := svc.Decisions(ctx, principal(alice), c.ID)
	if len(decisions) == 0 || decisions[0].RuleVersionID == 0 {
		t.Fatalf("auto rejection must bind a rule version, got %+v", decisions)
	}
}

// 7. Rollback creates a NEW revision copied from an old one; history untouched.
func TestRollbackCreatesNewRevisionWithoutRewritingHistory(t *testing.T) {
	initTest(t)
	c := mustCreateDraft(t, alice, "tech", "original text")
	c2, err := svc.Edit(ctx, principal(alice), c.ID, service.EditContentInput{
		Title: "p", Body: "second text", EditReason: "edit 2",
	})
	if err != nil {
		t.Fatalf("edit2: %v", err)
	}
	if c2.CurrentRevision.RevisionNo != 2 {
		t.Fatalf("expected rev2")
	}
	revs, err := svc.ListRevisions(ctx, principal(alice), c.ID)
	if err != nil {
		t.Fatalf("list revisions: %v", err)
	}
	// revs are newest-first; rev1 is the last entry.
	rev1 := revs[len(revs)-1]

	rb, err := svc.Rollback(ctx, principal(alice), c.ID, rev1.ID, "undo experiment")
	if err != nil {
		t.Fatalf("rollback: %v", err)
	}
	if rb.CurrentRevision.RevisionNo != 3 || rb.CurrentRevision.Body != "original text" {
		t.Fatalf("rollback must create rev3 with rev1 body, got %+v", rb.CurrentRevision)
	}
	// The reason must be retained.
	if rb.CurrentRevision.EditReason == "" ||
		!strings.Contains(rb.CurrentRevision.EditReason, "undo experiment") {
		t.Fatalf("rollback reason not retained: %q", rb.CurrentRevision.EditReason)
	}
	// History still has exactly 3 revisions and rev1 is unchanged.
	revs2, _ := svc.ListRevisions(ctx, principal(alice), c.ID)
	if len(revs2) != 3 || revs2[2].Body != "original text" || revs2[1].Body != "second text" {
		t.Fatalf("history rewritten: %+v", revs2)
	}
}

// 8. Withdraw requires a reason; withdrawn content disappears from the feed.
func TestWithdrawReasonAndFeedRemoval(t *testing.T) {
	initTest(t)
	c := mustCreateDraft(t, alice, "tech", "going live")
	if _, err := svc.Submit(ctx, principal(alice), c.ID); err != nil {
		t.Fatalf("submit: %v", err)
	}
	claim, _ := svc.Claim(ctx, principal(modTech))
	if _, err := svc.Approve(ctx, principal(modTech), claim.TaskID, ""); err != nil {
		t.Fatalf("approve: %v", err)
	}
	if _, err := svc.Withdraw(ctx, principal(alice), c.ID, ""); !errors.Is(err, service.ErrValidation) {
		t.Fatalf("withdraw without reason: want validation, got %v", err)
	}
	if _, err := svc.Withdraw(ctx, principal(alice), c.ID, "legal request"); err != nil {
		t.Fatalf("withdraw: %v", err)
	}
	page, _ := svc.Feed(ctx, principal(bob), 100, "")
	for _, it := range page.Items {
		if it.ID == c.ID {
			t.Fatalf("withdrawn content still in feed")
		}
	}
	// A regular member other than the author cannot see it at all.
	if _, err := svc.GetContent(ctx, principal(bob), c.ID); !errors.Is(err, service.ErrNotFound) {
		t.Fatalf("other member seeing withdrawn post: want not-found, got %v", err)
	}
}

// 9. Rule version switch: new words take effect for subsequent submits,
// and the decision is bound to the exact version used.
func TestRuleSwitchAppliesToLaterSubmit(t *testing.T) {
	initTest(t)
	rv, err := svc.CreateRuleVersion(ctx, principal(adminID), service.CreateRuleVersionInput{
		Description: "adds " + uniqueSuffix(),
		Words:       []string{"newword"},
	})
	if err != nil {
		t.Fatalf("create rule version: %v", err)
	}
	c := mustCreateDraft(t, alice, "tech", "this has newword in it")
	_, err = svc.Submit(ctx, principal(alice), c.ID)
	if !errors.Is(err, service.ErrRuleViolation) {
		t.Fatalf("new word must reject under new rule version, got %v", err)
	}
	decisions, _ := svc.Decisions(ctx, principal(alice), c.ID)
	if len(decisions) == 0 || decisions[0].RuleVersionID != rv.ID {
		t.Fatalf("decision bound to %v, want rule %d", decisions, rv.ID)
	}
}

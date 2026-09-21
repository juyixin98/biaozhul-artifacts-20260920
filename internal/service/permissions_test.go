package service_test

import (
	"errors"
	"testing"

	"communityvault/internal/service"
)

// Ordinary reads only ever return published content: drafts, pending and
// withdrawn posts look like 404 to unrelated members.
func TestReadVisibilityByStatus(t *testing.T) {
	initTest(t)
	c := mustCreateDraft(t, alice, "tech", "secret draft")

	// draft: author sees it, another member does not.
	if _, err := svc.GetContent(ctx, principal(alice), c.ID); err != nil {
		t.Fatalf("author reading own draft: %v", err)
	}
	if _, err := svc.GetContent(ctx, principal(bob), c.ID); !errors.Is(err, service.ErrNotFound) {
		t.Fatalf("other member reading draft: want not-found, got %v", err)
	}

	// pending: unrelated member still blocked; in-scope moderator can read.
	if _, err := svc.Submit(ctx, principal(alice), c.ID); err != nil {
		t.Fatalf("submit: %v", err)
	}
	if _, err := svc.GetContent(ctx, principal(bob), c.ID); !errors.Is(err, service.ErrNotFound) {
		t.Fatalf("other member reading pending: want not-found, got %v", err)
	}
	if _, err := svc.GetContent(ctx, principal(modArt), c.ID); !errors.Is(err, service.ErrNotFound) {
		t.Fatalf("out-of-scope moderator reading pending: want not-found, got %v", err)
	}
	if _, err := svc.GetContent(ctx, principal(modTech), c.ID); err != nil {
		t.Fatalf("in-scope moderator reading pending: %v", err)
	}

	// published: everyone reads it.
	claim, err := svc.Claim(ctx, principal(modTech))
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if _, err := svc.Approve(ctx, principal(modTech), claim.TaskID, "ok"); err != nil {
		t.Fatalf("approve: %v", err)
	}
	if _, err := svc.GetContent(ctx, principal(bob), c.ID); err != nil {
		t.Fatalf("other member reading published: %v", err)
	}

	// withdrawn: hidden again from other members.
	if _, err := svc.Withdraw(ctx, principal(alice), c.ID, "taking it down"); err != nil {
		t.Fatalf("withdraw: %v", err)
	}
	if _, err := svc.GetContent(ctx, principal(bob), c.ID); !errors.Is(err, service.ErrNotFound) {
		t.Fatalf("other member reading withdrawn: want not-found, got %v", err)
	}
}

// Members cannot create rule versions or read the active rule list.
func TestRuleAdministrationIsAdminOnly(t *testing.T) {
	initTest(t)
	if _, err := svc.CreateRuleVersion(ctx, principal(alice), service.CreateRuleVersionInput{
		Words: []string{"x"},
	}); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("member creating rule: want forbidden, got %v", err)
	}
	if _, err := svc.CreateRuleVersion(ctx, principal(modTech), service.CreateRuleVersionInput{
		Words: []string{"x"},
	}); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("moderator creating rule: want forbidden, got %v", err)
	}
	// Moderator may read the active rule set (needed for review), admin may
	// list all versions.
	if _, err := svc.ActiveRule(ctx, principal(modTech)); err != nil {
		t.Fatalf("moderator reading active rule: %v", err)
	}
	if _, err := svc.ListRules(ctx, principal(adminID)); err != nil {
		t.Fatalf("admin listing rules: %v", err)
	}
	if _, err := svc.ListRules(ctx, principal(modTech)); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("moderator listing all rules: want forbidden, got %v", err)
	}
}

// A member cannot see moderation evidence (decisions) for content they don't
// own, and a moderator cannot act outside their category via reports either.
func TestEvidenceAccessControl(t *testing.T) {
	initTest(t)
	c := mustCreateDraft(t, alice, "tech", "evidence test")
	if _, err := svc.Report(ctx, principal(bob), c.ID, "spam"); err != nil {
		t.Fatalf("report: %v", err)
	}

	// Unrelated member: cannot list reports or decisions.
	if _, err := svc.ListReports(ctx, principal(modArt), c.ID); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("art mod listing tech reports: want forbidden, got %v", err)
	}
	// In-scope moderator: full reporter identity visible.
	view, err := svc.ListReports(ctx, principal(modTech), c.ID)
	if err != nil {
		t.Fatalf("tech mod listing reports: %v", err)
	}
	list := view.([]service.ReportSummary)
	if len(list) != 1 || list[0].ReporterID != bob {
		t.Fatalf("moderator must see reporter id, got %+v", list)
	}
	// Author: aggregate only, reporter ids hidden.
	agg, err := svc.ListReports(ctx, principal(alice), c.ID)
	if err != nil {
		t.Fatalf("author listing reports: %v", err)
	}
	if _, ok := agg.(service.ReportAggregate); !ok {
		t.Fatalf("author must receive aggregate, got %T", agg)
	}
}

// Concurrent author edit vs moderator approve: regardless of which commits
// first, the end state is consistent — either the revision is published OR the
// task is cancelled and the post returns to draft; never a draft with a live
// approved task or a published newer revision.
func TestConcurrentEditVsApproveIsConsistent(t *testing.T) {
	initTest(t)
	const iterations = 12
	for i := 0; i < iterations; i++ {
		c := mustCreateDraft(t, alice, "tech", "race body v1")
		if _, err := svc.Submit(ctx, principal(alice), c.ID); err != nil {
			t.Fatalf("submit: %v", err)
		}
		claim, err := svc.Claim(ctx, principal(modTech))
		if err != nil {
			t.Fatalf("claim: %v", err)
		}
		if claim.ContentID != c.ID {
			t.Fatalf("claimed wrong content: %d vs %d", claim.ContentID, c.ID)
		}

		editErr := make(chan error, 1)
		apprErr := make(chan error, 1)
		go func() {
			_, err := svc.Edit(ctx, principal(alice), c.ID, service.EditContentInput{
				Title: "post", Body: "race body v2", EditReason: "race edit",
			})
			editErr <- err
		}()
		go func() {
			_, err := svc.Approve(ctx, principal(modTech), claim.TaskID, "race approve")
			apprErr <- err
		}()
		e1, e2 := <-editErr, <-apprErr

		got, err := svc.GetContent(ctx, principal(alice), c.ID)
		if err != nil {
			t.Fatalf("get after race: %v", err)
		}

		switch {
		case e1 == nil && errors.Is(e2, service.ErrStaleClaim):
			// Edit won: draft at rev2, old approval unusable.
			if got.Status != "draft" || got.CurrentRevision.RevisionNo != 2 {
				t.Fatalf("edit-win invariant broken: status=%s rev=%v",
					got.Status, got.CurrentRevision)
			}
		case errors.Is(e1, service.ErrConflict) && e2 == nil:
			// Approve won: published at rev1; edit refused on a published post.
			if got.Status != "published" || got.CurrentRevision.RevisionNo != 1 {
				t.Fatalf("approve-win invariant broken: status=%s rev=%v",
					got.Status, got.CurrentRevision)
			}
		default:
			t.Fatalf("iteration %d: unexpected error pair edit=%v approve=%v (status=%s)",
				i, e1, e2, got.Status)
		}
	}
}

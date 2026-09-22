package integration

import (
	"testing"

	"communityvault/internal/db"
	"communityvault/internal/view"
)

func anon() view.Viewer { return view.Viewer{} }

// The public feed only ever shows published content and published bodies;
// keyset paging stays stable when rows are inserted or withdrawn while the
// reader walks pages.
func TestFeedStableCursorAndNoLeak(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")

	// Publish 5 items.
	var ids []int64
	for i := 0; i < 5; i++ {
		c, _ := e.submitPublished(t, alice, 1, "pub", "published body")
		ids = append(ids, c.ID)
	}
	// Plus an unpublished draft and a pending item: must never appear.
	draft, _, _ := e.cts.Create(e.ctx, alice.ID, 1, "draft post", "draft body")
	pending, _, _ := e.cts.Create(e.ctx, alice.ID, 1, "pending post", "pending body")
	if _, _, _, err := e.cts.Submit(e.ctx, alice.ID, pending.ID); err != nil {
		t.Fatal(err)
	}

	// Page 1: 3 newest published.
	p1, err := e.vs.Feed(e.ctx, 0, 3)
	if err != nil {
		t.Fatal(err)
	}
	if len(p1.Items) != 3 || !p1.HasMore {
		t.Fatalf("page1 = %d items hasmore=%v", len(p1.Items), p1.HasMore)
	}
	seen := map[int64]bool{}
	for _, it := range p1.Items {
		if it.Body != "published body" || it.ID == draft.ID || it.ID == pending.ID {
			t.Fatalf("leak on page1: %+v", it)
		}
		seen[it.ID] = true
	}

	// Concurrent-with-paging changes: insert a NEW published item and
	// withdraw one of the rows the second page would otherwise contain.
	newest, _ := e.submitPublished(t, alice, 1, "new", "published body")
	if _, err := e.cts.Withdraw(e.ctx, alice, ids[1], "pulled mid-paging", nil); err != nil {
		t.Fatal(err)
	}

	// Page 2 using the cursor captured before those changes: the withdrawn
	// row is gone, the newly inserted (higher-id) row never appears here, and
	// nothing duplicates from page 1.
	p2, err := e.vs.Feed(e.ctx, p1.NextCursor, 3)
	if err != nil {
		t.Fatal(err)
	}
	if len(p2.Items) != 1 {
		t.Fatalf("page2 items = %d, want 1 (one surviving older row)", len(p2.Items))
	}
	for _, it := range p2.Items {
		if seen[it.ID] {
			t.Fatalf("duplicate across pages: %d", it.ID)
		}
		if it.ID == newest.ID {
			t.Fatal("newly inserted item leaked into the older page")
		}
		if it.ID == ids[1] {
			t.Fatal("withdrawn item leaked into the older page")
		}
		if it.ID != ids[0] {
			t.Fatalf("page2 row = %d, want oldest %d", it.ID, ids[0])
		}
	}
}

// Anonymous and member readers only see published content/revisions.
func TestPublicReadVisibility(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")
	bob := e.user(t, "token-bob")

	draft, _, _ := e.cts.Create(e.ctx, alice.ID, 1, "d", "draft body")
	if _, err := e.vs.GetContent(e.ctx, anon(), draft.ID); err == nil {
		t.Fatal("anonymous must not read a draft")
	}
	bobView := view.Viewer{User: &bob}
	if _, err := e.vs.GetContent(e.ctx, bobView, draft.ID); err == nil {
		t.Fatal("another member must not read a draft")
	}

	// Published content is readable by everyone at its published revision;
	// once withdrawn it disappears from the public immediately.
	c2, _ := e.submitPublished(t, alice, 1, "second", "clean published v1")
	got, err := e.vs.GetContent(e.ctx, anon(), c2.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got.Revision.Body != "clean published v1" {
		t.Fatalf("public body = %q", got.Revision.Body)
	}
	if _, err := e.cts.Withdraw(e.ctx, alice, c2.ID, "author pull", nil); err != nil {
		t.Fatal(err)
	}
	if _, err := e.vs.GetContent(e.ctx, anon(), c2.ID); err == nil {
		t.Fatal("withdrawn content must vanish from public reads")
	}

	// The public revision list contains only the published revision, never
	// draft history.
	c3, _ := e.submitPublished(t, alice, 1, "third", "only body")
	pubOnly, err := e.vs.Revisions(e.ctx, anon(), c3.ID)
	if err != nil || len(pubOnly) != 1 {
		t.Fatalf("public revisions = %v err=%v", pubOnly, err)
	}
}

// Staff/author views: the author sees all revisions but no reviewer
// identity; a scoped moderator sees full detail in their category and is
// blocked from the other category.
func TestRoleBasedReadAndRedaction(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")
	modTech := e.user(t, "token-mod-tech")
	modLife := e.user(t, "token-mod-life")

	c, _, err := e.cts.Create(e.ctx, alice.ID, 1, "a", "r1")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := e.cts.Edit(e.ctx, alice.ID, c.ID, "r2", "fix"); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := e.cts.Submit(e.ctx, alice.ID, c.ID); err != nil {
		t.Fatal(err)
	}
	task, _, err := e.mod.Claim(e.ctx, modTech, e.catIDs(t, modTech.ID))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := e.mod.Approve(e.ctx, modTech, task.ID, e.catIDs(t, modTech.ID)); err != nil {
		t.Fatal(err)
	}
	bob := e.user(t, "token-bob")
	if _, err := e.mod.Report(e.ctx, bob.ID, c.ID, "looks off"); err != nil {
		t.Fatal(err)
	}

	aliceView := view.Viewer{User: &alice}
	techView := view.Viewer{User: &modTech, ModeratorCategories: e.catIDs(t, modTech.ID)}
	lifeView := view.Viewer{User: &modLife, ModeratorCategories: e.catIDs(t, modLife.ID)}

	// Author: full revision history.
	revs, err := e.vs.Revisions(e.ctx, aliceView, c.ID)
	if err != nil || len(revs) != 2 {
		t.Fatalf("author revisions = %v err=%v", revs, err)
	}
	// Author: events/reviews visible but reviewer identity hidden.
	events, err := e.vs.Events(e.ctx, aliceView, c.ID)
	if err != nil {
		t.Fatal(err)
	}
	for _, ev := range events {
		if ev.ActorID != nil {
			t.Fatalf("author must not see actor ids: %+v", ev)
		}
	}
	reviews, err := e.vs.Reviews(e.ctx, aliceView, c.ID)
	if err != nil {
		t.Fatal(err)
	}
	for _, rv := range reviews {
		if rv.ReviewerID != nil {
			t.Fatalf("author must not see reviewer id: %+v", rv)
		}
	}
	// Authors never see the report list.
	if _, err := e.vs.ReportsForContent(e.ctx, aliceView, c.ID); err == nil {
		t.Fatal("author must not list reports")
	}

	// Tech moderator: full detail including identities.
	techReviews, err := e.vs.Reviews(e.ctx, techView, c.ID)
	if err != nil {
		t.Fatal(err)
	}
	for _, rv := range techReviews {
		if rv.ReviewerID == nil || *rv.ReviewerID != modTech.ID {
			t.Fatalf("moderator should see own reviewer id: %+v", rv)
		}
	}
	techReports, err := e.vs.ReportsForContent(e.ctx, techView, c.ID)
	if err != nil || len(techReports) != 1 {
		t.Fatalf("moderator reports = %v err=%v", techReports, err)
	}

	// Life moderator is scoped out of category 1 entirely.
	lifeDraft, _, _ := e.cts.Create(e.ctx, alice.ID, 1, "z", "z body")
	_ = lifeDraft
	if _, err := e.vs.Reviews(e.ctx, lifeView, c.ID); err == nil {
		t.Fatal("out-of-category moderator must be forbidden")
	}
}

// Moderator decisions are scoped to category even if a task id is guessed.
func TestModeratorCannotActOutsideCategory(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")
	// Pending task in life (category 2).
	c, _, _ := e.cts.Create(e.ctx, alice.ID, 2, "life", "body")
	_, _, _, _ = e.cts.Submit(e.ctx, alice.ID, c.ID)

	admin := e.user(t, "token-admin")
	task, _, err := e.mod.Claim(e.ctx, admin, nil)
	if err != nil {
		t.Fatal(err)
	}
	modTech := e.user(t, "token-mod-tech")
	// Hand the life task to the tech moderator at the DB level is not needed;
	// they simply attempt a decision on a task they do not own and are not
	// scoped to: both claim-ownership and category scope must reject.
	if _, err := e.mod.Approve(e.ctx, modTech, task.ID, e.catIDs(t, modTech.ID)); err == nil {
		t.Fatal("tech moderator must not resolve a life-category task")
	}
	// Task remains claimed, untouched.
	row, _ := db.New(e.pool).GetTaskForUpdate(e.ctx, task.ID)
	if row.Status != "claimed" {
		t.Fatalf("task status = %s, want claimed", row.Status)
	}
}

// Withdrawal: only the author or an in-category moderator/admin can do it.
func TestWithdrawPermissions(t *testing.T) {
	e := setup(t)
	alice := e.user(t, "token-alice")
	bob := e.user(t, "token-bob")
	c, _ := e.submitPublished(t, alice, 1, "p", "body")

	if _, err := e.cts.Withdraw(e.ctx, bob, c.ID, "not mine", nil); err == nil {
		t.Fatal("another member must not withdraw")
	}
	modLife := e.user(t, "token-mod-life")
	if _, err := e.cts.Withdraw(e.ctx, modLife, c.ID, "wrong cat", e.catIDs(t, modLife.ID)); err == nil {
		t.Fatal("out-of-category moderator must not withdraw")
	}
	modTech := e.user(t, "token-mod-tech")
	if _, err := e.cts.Withdraw(e.ctx, modTech, c.ID, "mod takedown", e.catIDs(t, modTech.ID)); err != nil {
		t.Fatalf("in-category moderator withdraw: %v", err)
	}
	got, _ := db.New(e.pool).GetContent(e.ctx, c.ID)
	if got.Status != "withdrawn" {
		t.Fatalf("status = %s", got.Status)
	}
}

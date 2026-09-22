package services_test

import (
	"testing"

	"communitygov/internal/services"
)

// TestReportLifecycleAndSingleAppeal walks filed -> accepted -> upheld
// (post taken down) -> appeal (exactly once) -> overturned -> restore,
// asserting post visibility and that all decisions record actor + basis.
func TestReportLifecycleAndSingleAppeal(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "rl-admin@x.io")
	author := f.mustRegister("member", "rl-author@x.io")
	reporter := f.mustRegister("member", "rl-reporter@x.io")
	c := f.mustCommunity("C", admin.ID)
	rev := f.ensureReviewer(c.ID)
	tier := f.mustTier(c.ID, 1, 30)

	p, v1 := publishPost(t, f, c.ID, author.ID, 1)
	// The reporter is also a paying member, so access checks exercise the real
	// tier+expiry gate rather than the reporter/author self-access bypass.
	f.pay(c.ID, reporter.ID, tier.ID, "reporter", 30)

	rep, err := f.report.File(f.ctx, c.ID, reporter.ID, p.ID, v1.ID, "spam", "looks like spam")
	if err != nil {
		t.Fatal(err)
	}

	if _, err := f.report.Accept(f.ctx, rep.ID, c.ID, rev.ID, "triaged"); err != nil {
		t.Fatal(err)
	}
	// Illegal transition: cannot appeal an accepted report.
	if _, err := f.report.Appeal(f.ctx, rep.ID, c.ID, author.ID, "early"); err == nil {
		t.Fatal("appeal before a ruling must be rejected")
	}

	// Uphold -> post immediately removed.
	if _, err := f.report.Uphold(f.ctx, rep.ID, c.ID, rev.ID, "confirmed spam"); err != nil {
		t.Fatal(err)
	}
	down, _ := f.posts.GetPost(f.ctx, p.ID)
	if down.Status != "removed" {
		t.Fatalf("uphold must takedown, got %s", down.Status)
	}
	if down.PublishedVersionID.Valid {
		t.Fatal("uphold must clear published_version_id")
	}
	if _, err := f.posts.AuthorizeVersion(f.ctx, v1.ID, readCtx(c.ID, reporter)); err == nil {
		t.Fatal("content taken down must be inaccessible to members")
	}

	// Author appeals (allowed once).
	if _, err := f.report.Appeal(f.ctx, rep.ID, c.ID, author.ID, "not spam, please review"); err != nil {
		t.Fatalf("first appeal must succeed: %v", err)
	}
	if _, err := f.report.Appeal(f.ctx, rep.ID, c.ID, author.ID, "again"); err == nil {
		t.Fatal("a report may be appealed at most once")
	}

	// Moderator overturns the ruling on appeal; content stays removed until an
	// explicit restore decision.
	if _, err := f.report.Overturn(f.ctx, rep.ID, c.ID, rev.ID, "false positive"); err != nil {
		t.Fatal(err)
	}
	stillDown, _ := f.posts.GetPost(f.ctx, p.ID)
	if stillDown.Status != "removed" {
		t.Fatal("overturn alone must not re-publish; restore is a separate decision")
	}

	restored, finalPost, err := f.report.Restore(f.ctx, rep.ID, c.ID, rev.ID, v1.ID, "appeal vindicates content")
	if err != nil {
		t.Fatalf("restore after overturn: %v", err)
	}
	if restored.Status != "restored" {
		t.Fatalf("report status should be restored, got %s", restored.Status)
	}
	if finalPost.Status != "published" || finalPost.PublishedVersionID.Int64 == 0 {
		t.Fatal("restore must re-publish content")
	}
	if finalPost.PublishedVersionID.Int64 != v1.ID {
		t.Fatal("restoring the newest approved version must re-point at v1")
	}

	// Access is back.
	if _, err := f.posts.AuthorizeVersion(f.ctx, v1.ID, readCtx(c.ID, reporter)); err != nil {
		t.Fatalf("restored content should be readable again: %v", err)
	}

	// Every step recorded an actor + reason.
	ds, err := f.report.Decisions(f.ctx, rep.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(ds) != 5 {
		t.Fatalf("expected accept/uphold/appeal/overturn/restore (5 decisions), got %d", len(ds))
	}
	for _, d := range ds {
		if d.Reason == "" {
			t.Fatal("every decision must carry a basis (reason)")
		}
	}
}

// TestOutsiderCannotAppeal: only the report parties (author or reporter) may
// appeal.
func TestOutsiderCannotAppeal(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "oa-admin@x.io")
	author := f.mustRegister("member", "oa-author@x.io")
	reporter := f.mustRegister("member", "oa-reporter@x.io")
	bystander := f.mustRegister("member", "oa-bystander@x.io")
	c := f.mustCommunity("C", admin.ID)
	rev := f.ensureReviewer(c.ID)
	p, v := publishPost(t, f, c.ID, author.ID, 1)
	rep, err := f.report.File(f.ctx, c.ID, reporter.ID, p.ID, v.ID, "abuse", "x")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.report.Accept(f.ctx, rep.ID, c.ID, rev.ID, "ok"); err != nil {
		t.Fatal(err)
	}
	if _, err := f.report.Uphold(f.ctx, rep.ID, c.ID, rev.ID, "ok"); err != nil {
		t.Fatal(err)
	}
	if _, err := f.report.Appeal(f.ctx, rep.ID, c.ID, bystander.ID, "me too"); err == nil ||
		services.HTTPStatus(err) != 403 {
		t.Fatalf("bystander appeal must be 403, got %v", err)
	}
}

// TestReportRequiresRealVersion: a report cannot target a version of another
// post.
func TestReportRequiresRealVersion(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "rv-admin@x.io")
	author := f.mustRegister("member", "rv-author@x.io")
	reporter := f.mustRegister("member", "rv-reporter@x.io")
	c := f.mustCommunity("C", admin.ID)
	f.ensureReviewer(c.ID)
	p, _ := publishPost(t, f, c.ID, author.ID, 1)
	_, otherV := publishPost(t, f, c.ID, author.ID, 1)
	if _, err := f.report.File(f.ctx, c.ID, reporter.ID, p.ID, otherV.ID, "spam", "x"); err == nil {
		t.Fatal("report must bind target version to the reported post")
	}
}

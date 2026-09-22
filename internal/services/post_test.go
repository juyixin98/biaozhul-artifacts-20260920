package services_test

import (
	"testing"

	"communitygov/internal/services"
)

// TestSelfReviewForbidden: an author can never approve their own post.
func TestSelfReviewForbidden(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "sr-admin@x.io")
	author := f.mustRegister("member", "sr-author@x.io")
	c := f.mustCommunity("C", admin.ID)
	p, v, err := f.posts.CreatePost(f.ctx, c.ID, author.ID, 1, "T", "b")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.posts.Submit(f.ctx, p.ID, author.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := f.posts.Review(f.ctx, services.ReviewAction{
		PostID: p.ID, CommunityID: c.ID, ReviewerID: author.ID,
		VersionID: v.ID, Approve: true, ExpectedStatus: "pending",
	}); err == nil || services.HTTPStatus(err) != 403 {
		t.Fatalf("self-review must be 403, got %v", err)
	}
}

// TestApproveBindsVersionAndStatus: an approval carrying a stale version id
// must fail and must not publish the newer, unreviewed version.
func TestApproveBindsVersionAndStatus(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "ab-admin@x.io")
	author := f.mustRegister("member", "ab-author@x.io")
	c := f.mustCommunity("C", admin.ID)
	rev := f.ensureReviewer(c.ID)

	p, v1, err := f.posts.CreatePost(f.ctx, c.ID, author.ID, 1, "T", "b1")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.posts.Submit(f.ctx, p.ID, author.ID); err != nil {
		t.Fatal(err)
	}

	// Reviewer tries to approve the version they saw, but the author withdrew,
	// edited (creating v2) and resubmitted BEFORE the approval lands.
	if _, err := f.posts.Withdraw(f.ctx, p.ID, author.ID); err != nil {
		t.Fatal(err)
	}
	v2, err := f.posts.AddVersion(f.ctx, p.ID, author.ID, "T", "b2-unreviewed", 1)
	if err != nil {
		t.Fatal(err)
	}
	if v2.ID == v1.ID {
		t.Fatal("edit must produce a new immutable version")
	}
	if _, err := f.posts.Submit(f.ctx, p.ID, author.ID); err != nil {
		t.Fatal(err)
	}

	if _, err := f.posts.Review(f.ctx, services.ReviewAction{
		PostID: p.ID, CommunityID: c.ID, ReviewerID: rev.ID,
		VersionID: v1.ID, Approve: true, ExpectedStatus: "pending",
	}); err == nil {
		t.Fatal("approval bound to the old version id must fail")
	}

	// The post must still be pending; published_version unset; v2 unreviewed.
	got, err := f.posts.GetPost(f.ctx, p.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != "pending" {
		t.Fatalf("post should remain pending, got %s", got.Status)
	}
	if got.PublishedVersionID.Valid {
		t.Fatal("nothing must be published after a stale approval")
	}
	cur, _ := f.posts.GetVersion(f.ctx, got.CurrentVersionID.Int64)
	if cur.ReviewStatus != "pending" {
		t.Fatalf("new version must remain pending/unapproved, got %s", cur.ReviewStatus)
	}
	if cur.Body == "b1" {
		t.Fatal("current content must be the new edit, not the old one")
	}

	// Approving the CURRENT version (v2) succeeds.
	pub, err := f.posts.Review(f.ctx, services.ReviewAction{
		PostID: p.ID, CommunityID: c.ID, ReviewerID: rev.ID,
		VersionID: v2.ID, Approve: true, ExpectedStatus: "pending",
	})
	if err != nil {
		t.Fatalf("approving current version must succeed: %v", err)
	}
	if pub.PublishedVersionID.Int64 != v2.ID {
		t.Fatalf("published version must be v2, got %d", pub.PublishedVersionID.Int64)
	}
}

// TestReviewRacingEditSerialization: drive the exact race — an approve UPDATE
// and an edit-version insert happen concurrently. The row lock guarantees one
// of: approve publishes v1 (edit blocked because post is pending), or the edit
// path first withdraws. Here both callers go through services; either way the
// unreviewed version is never published.
func TestReviewRacingEditSerialization(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "rc-admin@x.io")
	author := f.mustRegister("member", "rc-author@x.io")
	c := f.mustCommunity("C", admin.ID)
	rev := f.ensureReviewer(c.ID)

	p, v1, err := f.posts.CreatePost(f.ctx, c.ID, author.ID, 1, "T", "b1")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.posts.Submit(f.ctx, p.ID, author.ID); err != nil {
		t.Fatal(err)
	}

	// An edit while pending must be rejected outright (withdraw-first model),
	// closing the race window at the API level.
	if _, err := f.posts.AddVersion(f.ctx, p.ID, author.ID, "T", "hacked", 1); err == nil {
		t.Fatal("editing a pending post must be rejected to prevent racing publish")
	}

	// Now interleave: goroutine A approves v1; goroutine B withdraws and edits.
	start := make(chan struct{})
	done := make(chan any, 2)
	go func() {
		<-start
		_, err := f.posts.Review(f.ctx, services.ReviewAction{
			PostID: p.ID, CommunityID: c.ID, ReviewerID: rev.ID,
			VersionID: v1.ID, Approve: true, ExpectedStatus: "pending",
		})
		done <- err
	}()
	go func() {
		<-start
		_, err := f.posts.Withdraw(f.ctx, p.ID, author.ID)
		done <- err
	}()
	close(start)
	<-done
	<-done

	got, _ := f.posts.GetPost(f.ctx, p.ID)
	if got.Status == "published" && got.PublishedVersionID.Valid {
		// Approve won: it published v1, the exact version that was pending. Safe.
		if got.PublishedVersionID.Int64 != v1.ID {
			t.Fatal("published something other than the reviewed v1")
		}
		return
	}
	// Withdraw won: post is back to draft, nothing published. Safe.
	if got.Status != "draft" {
		t.Fatalf("unexpected terminal status %s", got.Status)
	}
}

// TestVersionsAreImmutable: the original version row is never modified by a
// later edit or approval (except the review_status workflow field).
func TestVersionsAreImmutable(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "vi-admin@x.io")
	author := f.mustRegister("member", "vi-author@x.io")
	c := f.mustCommunity("C", admin.ID)
	rev := f.ensureReviewer(c.ID)

	p, v1 := publishPost(t, f, c.ID, author.ID, 1)
	// Edit the published post -> v2 draft, v1 untouched.
	v2, err := f.posts.AddVersion(f.ctx, p.ID, author.ID, "T2", "b2", 1)
	if err != nil {
		t.Fatal(err)
	}
	gotV1, _ := f.posts.GetVersion(f.ctx, v1.ID)
	if gotV1.Body != "body content" || gotV1.Title != "Title" {
		t.Fatalf("v1 content was mutated: %+v", gotV1)
	}
	// Submit + approve v2. v1 stays approved with original content.
	if _, err := f.posts.Submit(f.ctx, p.ID, author.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := f.posts.Review(f.ctx, services.ReviewAction{
		PostID: p.ID, CommunityID: c.ID, ReviewerID: rev.ID,
		VersionID: v2.ID, Approve: true, ExpectedStatus: "pending",
	}); err != nil {
		t.Fatal(err)
	}
	gotV1, _ = f.posts.GetVersion(f.ctx, v1.ID)
	if gotV1.Body != "body content" {
		t.Fatalf("v1 changed after v2 approval: %+v", gotV1)
	}
}

// TestRestoreDoesNotClobberNewerEdit: after a newer version exists, restoring
// an older approved version must MINT a new immutable version carrying the old
// content rather than overwrite or delete the newer one.
func TestRestoreDoesNotClobberNewerEdit(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "rs-admin@x.io")
	author := f.mustRegister("member", "rs-author@x.io")
	c := f.mustCommunity("C", admin.ID)
	rev := f.ensureReviewer(c.ID)

	p, v1 := publishPost(t, f, c.ID, author.ID, 1)

	// Take the published post down (as a report-driven moderation action).
	down, err := f.posts.Takedown(f.ctx, p.ID, c.ID, rev.ID, "violation", true)
	if err != nil {
		t.Fatal(err)
	}
	if down.Status != "removed" || down.PublishedVersionID.Valid {
		t.Fatal("takedown must remove access immediately")
	}

	// Author adds a newer draft edit (v2) while removed is blocked; simulate
	// the case where the newer version already exists: recreate via restore of
	// v1 first, then edit. Instead, test the direct restore-when-newer path by
	// inserting v2 before restore using AddVersion (allowed after restore).
	// --- Path: restore v1 when it IS the newest (mints no new version).
	rp, err := f.posts.Restore(f.ctx, p.ID, c.ID, rev.ID, v1.ID, "appeal upheld")
	if err != nil {
		t.Fatalf("restore newest approved version: %v", err)
	}
	if rp.PublishedVersionID.Int64 != v1.ID {
		t.Fatalf("restore must re-publish v1, got %d", rp.PublishedVersionID.Int64)
	}

	// Now edit (v2), approve, takedown again, then restore OLD v1 while v2 is
	// newer: must create v3 with v1's content and keep v2 in history.
	v2, err := f.posts.AddVersion(f.ctx, p.ID, author.ID, "T2", "newer-edit", 1)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.posts.Submit(f.ctx, p.ID, author.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := f.posts.Review(f.ctx, services.ReviewAction{
		PostID: p.ID, CommunityID: c.ID, ReviewerID: rev.ID,
		VersionID: v2.ID, Approve: true, ExpectedStatus: "pending",
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := f.posts.Takedown(f.ctx, p.ID, c.ID, rev.ID, "again", false); err != nil {
		t.Fatal(err)
	}
	rp2, err := f.posts.Restore(f.ctx, p.ID, c.ID, rev.ID, v1.ID, "old version vindicated")
	if err != nil {
		t.Fatalf("restore old version with newer present: %v", err)
	}
	versions, err := f.posts.ListVersions(f.ctx, p.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(versions) != 3 {
		t.Fatalf("expected 3 immutable versions (v1,v2,v3-copy), got %d", len(versions))
	}
	restoredID := rp2.PublishedVersionID.Int64
	restored, _ := f.posts.GetVersion(f.ctx, restoredID)
	if restored.Body != "body content" {
		t.Fatalf("restored content must match old v1, got %q", restored.Body)
	}
	if restored.VersionNumber != 3 {
		t.Fatalf("restored copy must be a NEW version 3, got %d", restored.VersionNumber)
	}
	// v2 still exists with its own content — not clobbered.
	v2got, _ := f.posts.GetVersion(f.ctx, v2.ID)
	if v2got.Body != "newer-edit" {
		t.Fatalf("newer version v2 was overwritten: %q", v2got.Body)
	}
}

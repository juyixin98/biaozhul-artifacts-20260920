package services_test

import (
	"fmt"
	"testing"

	"communitygov/internal/database/sqlcgen"
	"communitygov/internal/middleware"
	"communitygov/internal/services"
)

// ensureReviewer creates (once per fixture) a reviewer account and assigns it
// to the given community.
func (f *fixture) ensureReviewer(communityID int64) sqlcgen.User {
	if !f.revMade {
		f.rev = f.mustRegister("reviewer", fmt.Sprintf("rev-%p@x.io", f))
		f.revMade = true
	}
	f.addReviewer(communityID, f.rev.ID)
	return f.rev
}

// publishPost creates a level-restricted post, submits it and approves it via
// a dedicated reviewer, returning the post and its approved version.
func publishPost(t *testing.T, f *fixture, communityID, author int64, level int32) (sqlcgen.Post, sqlcgen.PostVersion) {
	t.Helper()
	rev := f.ensureReviewer(communityID)

	p, v, err := f.posts.CreatePost(f.ctx, communityID, author, level, "Title", "body content")
	if err != nil {
		t.Fatalf("create post: %v", err)
	}
	if _, err := f.posts.Submit(f.ctx, p.ID, author); err != nil {
		t.Fatalf("submit: %v", err)
	}
	p, err = f.posts.Review(f.ctx, services.ReviewAction{
		PostID: p.ID, CommunityID: communityID, ReviewerID: rev.ID,
		VersionID: v.ID, Approve: true, Reason: "ok", ExpectedStatus: "pending",
	})
	if err != nil {
		t.Fatalf("approve: %v", err)
	}
	v, err = f.posts.GetVersion(f.ctx, v.ID)
	if err != nil {
		t.Fatal(err)
	}
	return p, v
}

func readCtx(communityID int64, u sqlcgen.User) services.ReadContext {
	return services.ReadContext{
		CommunityID: communityID,
		User: middleware.CurrentUser{
			ID: u.ID, Email: u.Email, Name: u.DisplayName, Role: u.Role,
		},
	}
}

func reviewerCtx(communityID int64, u sqlcgen.User) services.ReadContext {
	rc := readCtx(communityID, u)
	rc.IsReviewer = true
	return rc
}

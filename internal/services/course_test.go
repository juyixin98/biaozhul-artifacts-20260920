package services_test

import (
	"testing"

	"communitygov/internal/services"
)

// TestCoursePublishFreezesSnapshot: after publish, editing the draft structure
// does not change what learners see. Republishing produces a new snapshot only
// after every referenced content version is revalidated.
func TestCoursePublishFreezesSnapshot(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "cp-admin@x.io")
	author := f.mustRegister("member", "cp-author@x.io")
	c := f.mustCommunity("C", admin.ID)

	// Two approved posts to use as lesson content.
	_, v1 := publishPost(t, f, c.ID, author.ID, 1)
	_, v2 := publishPost(t, f, c.ID, author.ID, 1)

	course, err := f.course.Create(f.ctx, c.ID, "Go 101")
	if err != nil {
		t.Fatal(err)
	}
	structure := []services.ModuleInput{{
		Position: 1,
		Title:    "Basics",
		Lessons: []services.LessonInput{
			{Position: 1, Title: "Intro", ContentVersionID: v1.ID},
			{Position: 2, Title: "Next", ContentVersionID: v2.ID},
		},
	}}
	if err := f.course.SetStructure(f.ctx, course.ID, c.ID, structure); err != nil {
		t.Fatal(err)
	}
	if _, err := f.course.Publish(f.ctx, course.ID, c.ID, admin.ID); err != nil {
		t.Fatal(err)
	}
	view1, err := f.course.GetPublished(f.ctx, course.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(view1.Modules) != 1 || len(view1.Modules[0].Lessons) != 2 {
		t.Fatalf("snapshot1 structure wrong: %+v", view1.Modules)
	}

	// Edit the DRAFT only: published snapshot must be unchanged.
	structure[0].Lessons = structure[0].Lessons[:1] // remove second lesson
	structure[0].Title = "Basics v2"
	if err := f.course.SetStructure(f.ctx, course.ID, c.ID, structure); err != nil {
		t.Fatalf("draft edit after publish must be allowed: %v", err)
	}
	viewAgain, err := f.course.GetPublished(f.ctx, course.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(viewAgain.Modules[0].Lessons) != 2 || viewAgain.Modules[0].Title != "Basics" {
		t.Fatal("editing the draft after publish changed the served snapshot")
	}

	// Republishing the one-lesson draft produces a new frozen snapshot.
	if _, err := f.course.Publish(f.ctx, course.ID, c.ID, admin.ID); err != nil {
		t.Fatal(err)
	}
	view2, err := f.course.GetPublished(f.ctx, course.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(view2.Modules[0].Lessons) != 1 {
		t.Fatal("republish must serve the new one-lesson structure")
	}
}

// TestCoursePublishRevalidatesContent: after a referenced post is edited into
// a new draft (so the referenced version is no longer the live published one),
// republishing must fail validation.
func TestCoursePublishRevalidatesContent(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "cv-admin@x.io")
	author := f.mustRegister("member", "cv-author@x.io")
	viewer := f.mustRegister("member", "cv-viewer@x.io")
	c := f.mustCommunity("C", admin.ID)
	rev := f.ensureReviewer(c.ID)
	tier := f.mustTier(c.ID, 1, 30)

	p, v1 := publishPost(t, f, c.ID, author.ID, 1)
	f.pay(c.ID, viewer.ID, tier.ID, "viewer", 30)
	course, err := f.course.Create(f.ctx, c.ID, "Course")
	if err != nil {
		t.Fatal(err)
	}
	structure := []services.ModuleInput{{
		Position: 1, Title: "M",
		Lessons: []services.LessonInput{{Position: 1, Title: "L", ContentVersionID: v1.ID}},
	}}
	if err := f.course.SetStructure(f.ctx, course.ID, c.ID, structure); err != nil {
		t.Fatal(err)
	}
	if _, err := f.course.Publish(f.ctx, course.ID, c.ID, admin.ID); err != nil {
		t.Fatal(err)
	}

	// While the edit is only a draft (or under re-review), the OLD approved
	// version v1 keeps serving members and remains a valid reference, so the
	// snapshot referencing it is still consistent.
	v2, err := f.posts.AddVersion(f.ctx, p.ID, author.ID, "T", "b2", 1)
	if err != nil {
		t.Fatal(err)
	}
	after, _ := f.posts.GetPost(f.ctx, p.ID)
	if after.Status != "published" || after.PublishedVersionID.Int64 != v1.ID {
		t.Fatal("editing a live post must keep the old approved version serving")
	}
	// The unreviewed new version must not be readable by a paying member.
	if _, err := f.posts.AuthorizeVersion(f.ctx, v2.ID, readCtx(c.ID, viewer)); err == nil {
		t.Fatal("unreviewed new version must not be accessible")
	}

	// Submit and approve v2: published pointer atomically advances to v2.
	if _, err := f.posts.Submit(f.ctx, p.ID, author.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := f.posts.Review(f.ctx, services.ReviewAction{
		PostID: p.ID, CommunityID: c.ID, ReviewerID: rev.ID,
		VersionID: v2.ID, Approve: true, ExpectedStatus: "published",
	}); err != nil {
		t.Fatal(err)
	}
	// Republish must now FAIL: the draft still references v1, which is no
	// longer the live published version.
	if _, err := f.course.Publish(f.ctx, course.ID, c.ID, admin.ID); err == nil {
		t.Fatal("republish referencing a superseded version must fail revalidation")
	}
	// Point the draft at the live v2 and republish succeeds.
	structure[0].Lessons[0].ContentVersionID = v2.ID
	if err := f.course.SetStructure(f.ctx, course.ID, c.ID, structure); err != nil {
		t.Fatal(err)
	}
	if _, err := f.course.Publish(f.ctx, course.ID, c.ID, admin.ID); err != nil {
		t.Fatalf("republish against an approved live version must succeed: %v", err)
	}
}

// TestCoursePublishedSnapshotSurvivesDraftEdits: the structure can always be
// edited (it's the draft), but the served snapshot only changes atomically on
// a validated republish. This is the "之后编辑不改变已发布课程" guarantee.
func TestCoursePublishedSnapshotSurvivesDraftEdits(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "ci-admin@x.io")
	author := f.mustRegister("member", "ci-author@x.io")
	c := f.mustCommunity("C", admin.ID)
	_, v1 := publishPost(t, f, c.ID, author.ID, 1)
	course, _ := f.course.Create(f.ctx, c.ID, "C")
	if err := f.course.SetStructure(f.ctx, course.ID, c.ID, []services.ModuleInput{{
		Position: 1, Title: "M",
		Lessons: []services.LessonInput{{Position: 1, Title: "L", ContentVersionID: v1.ID}},
	}}); err != nil {
		t.Fatal(err)
	}
	if _, err := f.course.Publish(f.ctx, course.ID, c.ID, admin.ID); err != nil {
		t.Fatal(err)
	}

	// Editing the draft while published is allowed...
	if err := f.course.SetStructure(f.ctx, course.ID, c.ID, []services.ModuleInput{}); err != nil {
		t.Fatalf("draft structure must stay editable after publish: %v", err)
	}
	// ...but republishing an empty structure is fine and replaces the snapshot
	// atomically; before that, the old snapshot still serves one lesson.
	view, err := f.course.GetPublished(f.ctx, course.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(view.Modules) != 1 {
		t.Fatal("published snapshot must be unchanged by draft edits")
	}
}

package integration

import (
	"encoding/base64"
	"net/http"
	"sync"
	"testing"
)

// TestReviewRaceEditAndApprove: while v1 is pending review, the author creates
// v2 and a moderator concurrently approves bound to v1. Exactly one of these
// is the winner; the invariant is that the unreviewed v2 is NEVER published.
func TestReviewRaceEditAndApprove(t *testing.T) {
	e := newEnv(t)
	_, modTok := e.moderatorUser()
	c := e.authorContent("race-author", 1, "v1 body")
	e.submit(c)

	var wg sync.WaitGroup
	var approveStatus int
	wg.Add(2)
	go func() { // author edits after submission
		defer wg.Done()
		_, _, _ = e.addVersion(c, "v2 unreviewed body")
	}()
	go func() { // moderator approves the reviewed v1
		defer wg.Done()
		approveStatus, _ = e.approve(modTok, c, c.v1, "reviewed v1")
	}()
	wg.Wait()

	switch approveStatus {
	case http.StatusOK:
		// Approve won: published pointer must be v1 even though v2 exists.
		st, b := doJSON(t, http.MethodGet,
			"/contents/"+itoa64(c.id), modTok, nil)
		mustStatus(t, st, 200, b)
		co := b["content"].(map[string]any)
		if asInt64(co["published_version_id"]) != c.v1 {
			t.Fatalf("approve won but published_version=%v, want v1 %d",
				co["published_version_id"], c.v1)
		}
	case http.StatusConflict:
		// Edit won: content bounced back to draft; nothing is published.
		st, b := doJSON(t, http.MethodGet,
			"/contents/"+itoa64(c.id), modTok, nil)
		mustStatus(t, st, 200, b)
		co := b["content"].(map[string]any)
		if co["status"] != "draft" {
			t.Fatalf("edit won but status=%v, want draft", co["status"])
		}
		if co["published_version_id"] != nil {
			t.Fatalf("unreviewed v2 must not be published, got %v",
				co["published_version_id"])
		}
	default:
		t.Fatalf("approve status=%d, want 200 or 409", approveStatus)
	}

	// Final invariant under every interleaving: v2 was never approved/published.
	_, vb := doJSON(t, http.MethodGet,
		"/contents/"+itoa64(c.id)+"/versions", modTok, nil)
	for _, raw := range vb["versions"].([]any) {
		v := raw.(map[string]any)
		if asInt64(v["version_no"]) == 2 && v["review_status"] == "approved" {
			t.Fatal("unreviewed new version v2 ended up approved")
		}
	}
}

// TestAuthorCannotSelfApprove: approval by the content's author is forbidden.
func TestAuthorCannotSelfApprove(t *testing.T) {
	e := newEnv(t)
	_, _ = e.moderatorUser()
	c := e.authorContent("self-author", 1, "body")
	e.submit(c)
	st, b := e.approve(c.author, c, c.v1, "self approve")
	mustStatus(t, st, http.StatusForbidden, b)
}

// TestVersionsImmutable: adding v2 never modifies v1; bodies are retained.
func TestVersionsImmutable(t *testing.T) {
	e := newEnv(t)
	_, modTok := e.moderatorUser()
	c := e.publishFlow(modTok, 1, "first body")
	v2, st, b := e.addVersion(c, "second body")
	mustStatus(t, st, 201, b)
	_ = v2

	_, v1b := doJSON(t, http.MethodGet, "/versions/"+itoa64(c.v1), modTok, nil)
	if got := v1b["version"].(map[string]any)["body"]; got != "first body" {
		t.Fatalf("v1 body mutated to %v", got)
	}
	_, v2b := doJSON(t, http.MethodGet, "/versions/"+itoa64(v2), modTok, nil)
	if got := v2b["version"].(map[string]any)["body"]; got != "second body" {
		t.Fatalf("v2 body = %v", got)
	}
}

// TestRestoreCannotClobberNewerEdit: after a takedown the author edits v2;
// restoring the old v1 must be rejected (409), so old content never overwrites
// the newer modification. Restoring without a newer edit succeeds.
func TestRestoreCannotClobberNewerEdit(t *testing.T) {
	e := newEnv(t)
	_, modTok := e.moderatorUser()
	c := e.publishFlow(modTok, 1, "published v1")

	// Uphold a report -> delisted.
	st, rb := doJSON(t, http.MethodPost,
		"/contents/"+itoa64(c.id)+"/reports", c.author,
		map[string]any{"version_id": c.v1, "category": "abuse", "reason": "test report"})
	mustStatus(t, st, 201, rb)
	rid := asInt64(rb["id"])
	st, b := doJSON(t, http.MethodPost, "/reports/"+itoa64(rid)+"/uphold", modTok,
		map[string]any{"basis": "violation confirmed"})
	mustStatus(t, st, 200, b)

	// Author creates a newer edit while delisted.
	v2, st, b := e.addVersion(c, "newer edit after takedown")
	mustStatus(t, st, 201, b)
	_ = v2

	// Attempt to restore the old v1 over the newer v2 -> 409.
	st, b = doJSON(t, http.MethodPost, "/contents/"+itoa64(c.id)+"/restore", modTok,
		map[string]any{"version_id": c.v1, "reason": "appeal succeeded"})
	mustStatus(t, st, http.StatusConflict, b)

	// The new v2 goes through normal review and becomes the published version;
	// v1 can never overwrite it.
	st, b = doJSON(t, http.MethodPost, "/contents/"+itoa64(c.id)+"/submit", c.author,
		map[string]any{"version_id": v2})
	mustStatus(t, st, 200, b)
	st, b = e.approve(modTok, c, v2, "newest version cleared")
	mustStatus(t, st, 200, b)
	if asInt64(b["published_version_id"]) != v2 {
		t.Fatalf("published_version=%v, want v2 %d", b["published_version_id"], v2)
	}
}

// TestBodyAttachmentExportConsistentIsolation: level-5 content is unreadable
// for a level-1 member through body, attachment download and export alike;
// after upgrade to level 5 all three work.
func TestBodyAttachmentExportConsistentIsolation(t *testing.T) {
	e := newEnv(t)
	_, modTok := e.moderatorUser()
	_ = modTok
	t1 := e.tier(1, 100, 30)
	t5 := e.tier(5, 500, 30)
	c := e.publishFlow(modTok, 5, "gold only body")

	// Attach a file (need a version still editable? published v1 is frozen;
	// attachment must be added before approval. Use a fresh flow instead.)
	c2 := e.authorContent("gold-with-file", 5, "gold file body")
	st, ab := doJSON(t, http.MethodPost, "/versions/"+itoa64(c2.v1)+"/attachments", c2.author,
		map[string]any{
			"filename":     "secret.txt",
			"content_type": "text/plain",
			"data_base64":  base64.StdEncoding.EncodeToString([]byte("gold-secret")),
		})
	mustStatus(t, st, 201, ab)
	attID := asInt64(ab["id"])
	e.submit(c2)
	st, _ = e.approve(modTok, c2, c2.v1, "ok")
	mustStatus(t, st, 200, nil)

	uid, memTok := e.createUser("lowreader", "member")
	st, _ = e.pay(uniqueReq("iso-1"), uid, t1, 100, 30)
	mustStatus(t, st, 200, nil)

	for _, tc := range []struct {
		name string
		path string
	}{
		{"body", "/contents/" + itoa64(c2.id)},
		{"attachment", "/attachments/" + itoa64(attID)},
		{"export", "/contents/" + itoa64(c2.id) + "/export"},
		{"level5-body", "/contents/" + itoa64(c.id)},
	} {
		st, b := doJSON(t, http.MethodGet, tc.path, memTok, nil)
		if st == http.StatusOK {
			t.Fatalf("level-1 member could access %s: %v", tc.name, b)
		}
	}

	// Upgrade to tier 5: renew at tier 5, all channels open.
	st, _ = e.pay(uniqueReq("iso-2"), uid, t5, 500, 30)
	mustStatus(t, st, 200, nil)
	st, b := doJSON(t, http.MethodGet, "/contents/"+itoa64(c2.id), memTok, nil)
	mustStatus(t, st, 200, b)
	st, b = doJSON(t, http.MethodGet, "/attachments/"+itoa64(attID), memTok, nil)
	mustStatus(t, st, 200, b)
	st, _ = doJSON(t, http.MethodGet, "/contents/"+itoa64(c2.id)+"/export", memTok, nil)
	mustStatus(t, st, 200, nil)
}

// TestMembersOnlySeePublished: drafts/pending and other versions are not
// readable by ordinary members.
func TestMembersOnlySeePublished(t *testing.T) {
	e := newEnv(t)
	_, modTok := e.moderatorUser()
	c := e.authorContent("hidden", 1, "draft body")
	uid, memTok := e.createUser("outsider", "member")
	t1 := e.tier(1, 100, 30)
	st, _ := e.pay(uniqueReq("hidden-pay"), uid, t1, 100, 30)
	mustStatus(t, st, 200, nil)

	st, _ = doJSON(t, http.MethodGet, "/contents/"+itoa64(c.id), memTok, nil)
	if st == http.StatusOK {
		t.Fatal("member read a draft content")
	}
	st, _ = doJSON(t, http.MethodGet, "/versions/"+itoa64(c.v1), memTok, nil)
	if st == http.StatusOK {
		t.Fatal("member read a draft version")
	}

	e.submit(c)
	st, _ = doJSON(t, http.MethodGet, "/contents/"+itoa64(c.id), memTok, nil)
	if st == http.StatusOK {
		t.Fatal("member read pending content")
	}
	st, _ = e.approve(modTok, c, c.v1, "ok")
	mustStatus(t, st, 200, nil)
	st, b := doJSON(t, http.MethodGet, "/contents/"+itoa64(c.id), memTok, nil)
	mustStatus(t, st, 200, b)
}

func itoa64(v int64) string {
	return formatInt(v)
}

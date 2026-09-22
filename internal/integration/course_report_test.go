package integration

import (
	"net/http"
	"strconv"
	"testing"
)

func formatInt(v int64) string { return strconv.FormatInt(v, 10) }

// TestBootstrapTokenScoped: the platform bootstrap token may create
// communities but must be rejected by every community-scoped route.
func TestBootstrapTokenScoped(t *testing.T) {
	st, b := doJSON(t, http.MethodGet, "/communities/999/tiers", "tok_bootstrap", nil)
	if st != http.StatusNotFound {
		t.Fatalf("bootstrap token reached scoped route: %d %v", st, b)
	}
	st, b = doJSON(t, http.MethodPost, "/admin/communities", "tok_wrong",
		map[string]any{"name": "x", "admin_name": "y"})
	if st != http.StatusUnauthorized {
		t.Fatalf("bad token status=%d", st)
	}
}

// TestCoursePublishFreezes: edits to the draft after publish do not change the
// published snapshot; republish validates every lesson again.
func TestCoursePublishFreezes(t *testing.T) {
	e := newEnv(t)
	_, modTok := e.moderatorUser()
	t1 := e.tier(1, 100, 30)
	c := e.publishFlow(modTok, 1, "course lesson body v1")

	// Create course with one module/lesson pointing at the published version.
	st, b := doJSON(t, http.MethodPost,
		"/communities/"+itoa64(e.cid)+"/courses", c.author, map[string]any{
			"title":          "C1",
			"required_level": 1,
			"modules": []map[string]any{{
				"position": 1, "title": "M1",
				"lessons": []map[string]any{{
					"position": 1, "title": "L1", "content_version_id": c.v1,
				}},
			}},
		})
	mustStatus(t, st, 201, b)
	courseID := asInt64(b["id"])

	st, b = doJSON(t, http.MethodPost, "/courses/"+itoa64(courseID)+"/publish",
		c.author, nil)
	mustStatus(t, st, 201, b)

	// Published snapshot references v1.
	st, b = doJSON(t, http.MethodGet, "/courses/"+itoa64(courseID), modTok, nil)
	mustStatus(t, st, 200, b)
	structure := b["structure"].([]any)
	firstMod := structure[0].(map[string]any)
	firstLesson := firstMod["lessons"].([]any)[0].(map[string]any)
	if asInt64(firstLesson["content_version_id"]) != c.v1 {
		t.Fatalf("published snapshot version = %v, want v1", firstLesson["content_version_id"])
	}

	// Author edits draft structure after publish.
	st, _ = doJSON(t, http.MethodPut, "/courses/"+itoa64(courseID)+"/structure",
		c.author, map[string]any{
			"modules": []map[string]any{{
				"position": 1, "title": "M1-renamed",
				"lessons": []map[string]any{{
					"position": 9, "title": "L9", "content_version_id": c.v1,
				}},
			}},
		})
	mustStatus(t, st, http.StatusNoContent, nil)

	// Reader-facing snapshot is unchanged.
	st, b = doJSON(t, http.MethodGet, "/courses/"+itoa64(courseID), modTok, nil)
	mustStatus(t, st, 200, b)
	structure = b["structure"].([]any)
	firstMod = structure[0].(map[string]any)
	if firstMod["title"] != "M1" {
		t.Fatalf("post-publish draft edit leaked into snapshot: %v", firstMod["title"])
	}

	// Lesson access for a level-1 member works via the frozen version.
	uid, memTok := e.createUser("student", "member")
	st, _ = e.pay(uniqueReq("course-pay"), uid, t1, 100, 30)
	mustStatus(t, st, 200, nil)
	st, b = doJSON(t, http.MethodGet,
		"/courses/"+itoa64(courseID)+"/lessons/1", memTok, nil)
	mustStatus(t, st, 200, b)
	if b["version"].(map[string]any)["body"] != "course lesson body v1" {
		t.Fatalf("lesson body = %v", b["version"])
	}
}

// TestCoursePublishRejectsUnpublishedOrTooHighLevel: republish must revalidate
// every referenced version.
func TestCoursePublishRejectsUnpublishedOrTooHighLevel(t *testing.T) {
	e := newEnv(t)
	_, modTok := e.moderatorUser()
	published := e.publishFlow(modTok, 1, "ok body")
	gold := e.authorContent("gold-author", 5, "gold body") // still draft

	st, b := doJSON(t, http.MethodPost,
		"/communities/"+itoa64(e.cid)+"/courses", published.author, map[string]any{
			"title":          "C-bad",
			"required_level": 1,
			"modules": []map[string]any{{
				"position": 1, "title": "M",
				"lessons": []map[string]any{
					{"position": 1, "title": "draft-lesson", "content_version_id": gold.v1},
				},
			}},
		})
	mustStatus(t, st, 201, b)
	courseID := asInt64(b["id"])

	// Draft version -> conflict.
	st, b = doJSON(t, http.MethodPost, "/courses/"+itoa64(courseID)+"/publish",
		published.author, nil)
	mustStatus(t, st, http.StatusConflict, b)

	// Even after the gold content is published at level 5, course level 1 may
	// not include it (tier mismatch).
	e.submit(gold)
	st, _ = e.approve(modTok, gold, gold.v1, "ok")
	mustStatus(t, st, 200, nil)
	st, b = doJSON(t, http.MethodPost, "/courses/"+itoa64(courseID)+"/publish",
		published.author, nil)
	mustStatus(t, st, http.StatusForbidden, b)
}

// TestReportAppealOnceAndAudit: full 受理 -> 裁决 -> 一次申诉 path with actor +
// basis retained on every step; a second appeal is rejected.
func TestReportAppealOnceAndAudit(t *testing.T) {
	e := newEnv(t)
	_, modTok := e.moderatorUser()
	c := e.publishFlow(modTok, 1, "reported body")

	// Another member reports.
	_, reporterTok := e.createUser("reporter-x", "member")
	st, b := doJSON(t, http.MethodPost, "/contents/"+itoa64(c.id)+"/reports",
		reporterTok,
		map[string]any{"version_id": c.v1, "category": "copyright", "reason": "疑似抄袭"})
	mustStatus(t, st, 201, b)
	rid := asInt64(b["id"])

	st, b = doJSON(t, http.MethodPost, "/reports/"+itoa64(rid)+"/accept", modTok,
		map[string]any{"basis": "已受理，进入核查"})
	mustStatus(t, st, 200, b)

	st, b = doJSON(t, http.MethodPost, "/reports/"+itoa64(rid)+"/uphold", modTok,
		map[string]any{"basis": "裁决违规依据-1"})
	mustStatus(t, st, 200, b)

	// Content immediately delisted.
	st, b = doJSON(t, http.MethodGet, "/contents/"+itoa64(c.id), modTok, nil)
	mustStatus(t, st, 200, b)
	if b["content"].(map[string]any)["status"] != "delisted" {
		t.Fatalf("after uphold content status = %v", b["content"])
	}

	// Author appeals once.
	st, b = doJSON(t, http.MethodPost, "/reports/"+itoa64(rid)+"/appeal", c.author,
		map[string]any{"basis": "申诉依据：拥有原创证明"})
	mustStatus(t, st, 200, b)

	// Second appeal rejected.
	st, b = doJSON(t, http.MethodPost, "/reports/"+itoa64(rid)+"/appeal", c.author,
		map[string]any{"basis": "再次申诉"})
	mustStatus(t, st, http.StatusConflict, b)

	// Moderator reverses on appeal -> status appeal_dismissed.
	st, b = doJSON(t, http.MethodPost, "/reports/"+itoa64(rid)+"/dismiss", modTok,
		map[string]any{"basis": "申诉成功依据：原创证明有效"})
	mustStatus(t, st, 200, b)
	if b["status"] != "appeal_dismissed" {
		t.Fatalf("status = %v, want appeal_dismissed", b["status"])
	}

	// Audit events retain actor and basis at each step.
	st, b = doJSON(t, http.MethodGet, "/reports/"+itoa64(rid)+"/events", modTok, nil)
	mustStatus(t, st, 200, b)
	evs := b["events"].([]any)
	actions := map[string]bool{}
	for _, raw := range evs {
		ev := raw.(map[string]any)
		actions[ev["action"].(string)] = true
		if ev["basis"] == "" && ev["action"] != "create" {
			t.Fatalf("event %v missing basis", ev["action"])
		}
		if asInt64(ev["actor_id"]) <= 0 {
			t.Fatalf("event %v missing actor", ev["action"])
		}
	}
	for _, want := range []string{"accept", "uphold", "appeal", "appeal_dismiss"} {
		if !actions[want] {
			t.Fatalf("audit trail missing action %q: %v", want, actions)
		}
	}
}

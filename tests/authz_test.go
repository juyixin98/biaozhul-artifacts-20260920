package tests_test

import (
	"net/http"
	"testing"

	"github.com/vfxqueue/renderq/internal/testutil"
)

// TestAuthRequired: no key -> 401, bad key -> 401.
func TestAuthRequired(t *testing.T) {
	h := testutil.New(t)
	for _, key := range []string{"", "wrong-key"} {
		st, _ := h.Do("GET", "/projects", key, nil)
		if st != http.StatusUnauthorized {
			t.Fatalf("key=%q: status=%d want 401", key, st)
		}
	}
}

// TestMembersIsolation: a member cannot read another project or its
// artifacts; admins can read everything.
func TestMembersIsolation(t *testing.T) {
	h := testutil.New(t)

	aliceProj := h.CreateProject(testutil.AliceKey, "alice-project")
	_ = h.CreateProject(testutil.BobKey, "bob-project")

	// Bob must not see Alice's project in his list (endpoint returns array).
	var list []map[string]any
	if err := decodeJSONBody(h, "GET", "/projects", testutil.BobKey, &list); err != nil {
		t.Fatal(err)
	}
	for _, p := range list {
		if p["id"] == aliceProj.String() {
			t.Fatal("bob can see alice's project")
		}
	}

	// Direct access is forbidden (403, not 404-leak).
	st, _ := h.Do("GET", "/projects/"+aliceProj.String(), testutil.BobKey, nil)
	if st != http.StatusForbidden {
		t.Fatalf("bob direct access: %d want 403", st)
	}

	// Asset listing of Alice's project is forbidden for Bob.
	st, _ = h.Do("GET", "/projects/"+aliceProj.String()+"/assets", testutil.BobKey, nil)
	if st != http.StatusForbidden {
		t.Fatalf("bob assets: %d want 403", st)
	}

	// Admin may access.
	st, _ = h.Do("GET", "/projects/"+aliceProj.String(), testutil.AdminKey, nil)
	if st != http.StatusOK {
		t.Fatalf("admin access: %d want 200", st)
	}
}

// TestAdminCanCancelAnyJob is exercised end-to-end in the cancel race test;
// here we check a non-owner member cannot cancel someone else's job.
func TestMemberCannotCancelForeignJob(t *testing.T) {
	h := testutil.New(t)
	proj, _, versionID := seedComposition(t, h, testutil.AliceKey)
	_ = proj

	st, body := h.Do("POST", "/versions/"+versionID.String()+"/jobs", testutil.AliceKey,
		map[string]any{"priority": 5, "frameStart": 0, "frameEnd": 0})
	if st != http.StatusCreated {
		t.Fatalf("create job: %d %v", st, body)
	}
	jobID := body["id"].(string)

	// Bob is not a member at all: 403 on read.
	st, _ = h.Do("POST", "/jobs/"+jobID+"/cancel", testutil.BobKey, nil)
	if st != http.StatusForbidden {
		t.Fatalf("bob cancel: %d want 403", st)
	}

	// Admin cancel is accepted.
	st, body = h.Do("POST", "/jobs/"+jobID+"/cancel", testutil.AdminKey, nil)
	if st != http.StatusOK {
		t.Fatalf("admin cancel: %d %v", st, body)
	}
	if body["status"] != "canceled" {
		t.Fatalf("status = %v", body["status"])
	}
}

// TestSharedMemberAccess: once Alice adds Bob to her project, Bob can read
// and enqueue work but still cannot cancel (only owner/admin).
func TestSharedMemberAccess(t *testing.T) {
	h := testutil.New(t)
	proj, compID, versionID := seedComposition(t, h, testutil.AliceKey)

	st, _ := h.Do("POST", "/projects/"+proj.String()+"/members", testutil.AliceKey,
		map[string]string{"username": "bob"})
	if st != http.StatusNoContent && st != http.StatusOK {
		t.Fatalf("add member: %d", st)
	}

	st, body := h.Do("GET", "/projects/"+proj.String(), testutil.BobKey, nil)
	if st != http.StatusOK {
		t.Fatalf("bob read after share: %d", st)
	}

	st, body = h.Do("POST", "/versions/"+versionID.String()+"/jobs", testutil.BobKey,
		map[string]any{"priority": 5, "frameStart": 0, "frameEnd": 1})
	if st != http.StatusCreated {
		t.Fatalf("bob enqueue: %d %v", st, body)
	}
	jobID := body["id"].(string)
	_ = compID

	st, _ = h.Do("POST", "/jobs/"+jobID+"/cancel", testutil.BobKey, nil)
	if st != http.StatusForbidden {
		t.Fatalf("non-owner member cancel: %d want 403", st)
	}
}

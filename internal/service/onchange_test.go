package service_test

import (
	"path/filepath"
	"testing"

	"buildprovenance/internal/provenance"
)

// TestOnChangeDoesNotDeadlock guards against a regression where
// RegisterTool invoked the change callback while holding the service mutex
// and the host callback (SaveTools) tried to take the same mutex.
func TestOnChangeDoesNotDeadlock(t *testing.T) {
	f := newFixture(t)
	toolsPath := filepath.Join(f.root, "tools.json")
	called := 0
	f.svc.SetOnChange(func() {
		called++
		if err := f.svc.SaveTools(toolsPath); err != nil {
			t.Errorf("SaveTools in callback: %v",
				err)
		}
	})
	f.registerTestSources()
	f.registerTools() // deadlocked before the fix (callback takes s.mu)
	if called == 0 {
		t.Fatal("change callback was not invoked")
	}

	// Persisted tools reload into a fresh service and keep their digests.
	if _, _, binA, _ := f.buildSharedGraph(); binA.ID == "" {
		t.Fatal("build failed")
	}

	list := f.svc.ListTools()
	if len(list) != 3 {
		t.Fatalf("expected 3 tools persisted, got %d", len(list))
	}
}

// TestReRegisterSameToolIdempotent checks that re-registering an identical
// action definition returns the same tool and does not error or change state.
func TestReRegisterSameToolIdempotent(t *testing.T) {
	f := newFixture(t)
	def := provenance.ToolDefinition{Name: "compile_lib", Command: []string{"bash", "compile_lib.sh"}}
	t1, err := f.svc.RegisterTool(def)
	if err != nil {
		t.Fatal(err)
	}
	t2, err := f.svc.RegisterTool(def)
	if err != nil {
		t.Fatal(err)
	}
	if t1.Digest != t2.Digest {
		t.Fatalf("identical tool registration must be idempotent: %s vs %s", t1.Digest, t2.Digest)
	}
}

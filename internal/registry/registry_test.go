package registry

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/p079/telegw/internal/convert"
)

func TestLoadAndHotSwap(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "mappings.json")
	content := `{"mappings":[
	  {"version":"strict","enum_policy":"error","rounding_policy":"strict"},
	  {"version":"carry","enum_policy":"carry","rounding_policy":"half_even"}
	]}`
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	reg, err := Load(path, "strict")
	if err != nil {
		t.Fatal(err)
	}
	if got := reg.Active().Version; got != "strict" {
		t.Fatalf("active = %q", got)
	}
	if err := reg.Activate("carry"); err != nil {
		t.Fatal(err)
	}
	if got := reg.Active().Version; got != "carry" {
		t.Fatalf("after swap active = %q", got)
	}
	if err := reg.Activate("missing"); err == nil {
		t.Fatal("expected error for unknown version")
	}
	// Active mapping must be untouched by the failed swap.
	if got := reg.Active().Version; got != "carry" {
		t.Fatalf("failed swap changed active to %q", got)
	}
}

// Empty version reloads the file and keeps the current version if still present.
func TestFileReloadKeepsActive(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "mappings.json")
	if err := os.WriteFile(path, []byte(`{"mappings":[
	  {"version":"a","enum_policy":"error","rounding_policy":"strict"},
	  {"version":"b","enum_policy":"carry","rounding_policy":"half_even"}
	]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	reg, err := Load(path, "a")
	if err != nil {
		t.Fatal(err)
	}
	if err := reg.Activate("b"); err != nil {
		t.Fatal(err)
	}
	// Rewrite the file (e.g. operator edited policies), reload with "".
	if err := os.WriteFile(path, []byte(`{"mappings":[
	  {"version":"a","enum_policy":"error","rounding_policy":"strict"},
	  {"version":"b","enum_policy":"error","rounding_policy":"strict"}
	]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := reg.Activate(""); err != nil {
		t.Fatal(err)
	}
	m := reg.Active()
	if m.Version != "b" || m.Enum != convert.EnumError {
		t.Fatalf("reloaded mapping = %+v, want b with error policy", m)
	}
}

func TestLoadRejectsInvalid(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "bad.json")
	if err := os.WriteFile(path, []byte(`{"mappings":[{"version":"x","enum_policy":"bogus","rounding_policy":"strict"}]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(path, "x"); err == nil {
		t.Fatal("expected validation error")
	}
}

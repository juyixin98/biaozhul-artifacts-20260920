// Package testutil builds fixture project trees for the test suite.
//
// Every executable under tools/ is a small /bin/sh script that the user
// (here: the test author) explicitly supplies as a fixture command. The
// service never invokes anything else.
package testutil

import (
	"os"
	"path/filepath"
	"testing"

	"bis/internal/spec"
	"bis/internal/store"
)

// ConcatTool is a deterministic concatenation fixture.
const ConcatTool = `#!/bin/sh
# Usage: concat.sh <output> <input>...
# Deterministic: output depends only on the named inputs' paths and bytes.
set -eu
out=$1
shift
mkdir -p "$(dirname "$out")"
: > "$out"
for f in "$@"; do
  printf '### %s\n' "$f" >> "$out"
  cat "$f" >> "$out"
  printf '\n' >> "$out"
done
`

// NondetTool embeds fresh randomness, so two runs never match byte-for-byte.
const NondetTool = `#!/bin/sh
# Usage: concat_nondet.sh <output> <input>...
# Non-reproducible on purpose: embeds a fresh UUID in every execution.
set -eu
out=$1
shift
mkdir -p "$(dirname "$out")"
{
  printf '### nondet %s\n' "$(cat /proc/sys/kernel/random/uuid)"
  for f in "$@"; do
    printf '### %s\n' "$f"
    cat "$f"
    printf '\n'
  done
} > "$out"
`

// FailTool always exits non-zero and writes no declared output.
const FailTool = `#!/bin/sh
echo "intentional fixture failure" >&2
exit 3
`

func writeFile(t *testing.T, path, content string, mode os.FileMode) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), mode); err != nil {
		t.Fatal(err)
	}
}

// NewStore opens a store in a temporary directory, cleaned up with the test.
func NewStore(t *testing.T) *store.Store {
	t.Helper()
	root := filepath.Join(t.TempDir(), "bis-root")
	st, err := store.Open(root)
	if err != nil {
		t.Fatal(err)
	}
	return st
}

// InstallProject writes a fixture project into the store's projects tree and
// returns its loaded spec.
func InstallProject(t *testing.T, st *store.Store, name string, buildJSON string, files map[string]string) *spec.Project {
	t.Helper()
	root := st.ProjectRoot(name)
	for rel, content := range files {
		writeFile(t, filepath.Join(root, rel), content, 0o644)
	}
	writeFile(t, filepath.Join(root, "tools", "concat.sh"), ConcatTool, 0o755)
	writeFile(t, filepath.Join(root, "tools", "concat_nondet.sh"), NondetTool, 0o755)
	writeFile(t, filepath.Join(root, "tools", "fail.sh"), FailTool, 0o755)
	writeFile(t, filepath.Join(root, "build.json"), buildJSON, 0o644)
	p, err := spec.Load(root)
	if err != nil {
		t.Fatalf("load fixture spec: %v", err)
	}
	return p
}

// SharedBuildJSON is a diamond: two compilers share one source, a linker
// consumes both artifacts.
const SharedBuildJSON = `{
  "name": "shared-demo",
  "actions": [
    {
      "id": "compile_a",
      "tool": "tools/concat.sh",
      "args": ["out/a.bundle", "src/shared.txt", "src/a.txt"],
      "sources": ["src/shared.txt", "src/a.txt"],
      "outputs": ["out/a.bundle"]
    },
    {
      "id": "compile_b",
      "tool": "tools/concat.sh",
      "args": ["out/b.bundle", "src/shared.txt", "src/b.txt"],
      "sources": ["src/shared.txt", "src/b.txt"],
      "outputs": ["out/b.bundle"]
    },
    {
      "id": "link",
      "tool": "tools/concat.sh",
      "args": ["out/final.txt", "in/a/out/a.bundle", "in/b/out/b.bundle"],
      "upstream": {"a": "compile_a", "b": "compile_b"},
      "outputs": ["out/final.txt"]
    }
  ]
}
`

// SharedFiles are the source inputs for SharedBuildJSON.
func SharedFiles() map[string]string {
	return map[string]string{
		"src/shared.txt": "COMMON HEADER\n",
		"src/a.txt":      "alpha-only line\n",
		"src/b.txt":      "bravo-only line\n",
	}
}

// InstallSharedProject creates the diamond fixture project.
func InstallSharedProject(t *testing.T, st *store.Store) *spec.Project {
	t.Helper()
	return InstallProject(t, st, "shared-demo", SharedBuildJSON, SharedFiles())
}

// NondetBuildJSON / NondetFiles define a single-action project whose output
// is not reproducible.
const NondetBuildJSON = `{
  "name": "nondet-demo",
  "actions": [
    {
      "id": "make",
      "tool": "tools/concat_nondet.sh",
      "args": ["out/x.txt", "src/s.txt"],
      "sources": ["src/s.txt"],
      "outputs": ["out/x.txt"]
    }
  ]
}
`

// InstallNondetProject creates the non-reproducible fixture project.
func InstallNondetProject(t *testing.T, st *store.Store) *spec.Project {
	t.Helper()
	return InstallProject(t, st, "nondet-demo", NondetBuildJSON, map[string]string{
		"src/s.txt": "stable source\n",
	})
}

// FailBuildJSON defines an action whose tool fails and emits no output.
const FailBuildJSON = `{
  "name": "fail-demo",
  "actions": [
    {
      "id": "boom",
      "tool": "tools/fail.sh",
      "outputs": ["out/nope.txt"]
    }
  ]
}
`

// InstallFailProject creates a failing fixture project.
func InstallFailProject(t *testing.T, st *store.Store) *spec.Project {
	t.Helper()
	return InstallProject(t, st, "fail-demo", FailBuildJSON, nil)
}

// TwoNodeBuildJSON is a simple two-action chain used by forgery tests.
const TwoNodeBuildJSON = `{
  "name": "two-demo",
  "actions": [
    {
      "id": "base",
      "tool": "tools/concat.sh",
      "args": ["out/base.txt", "src/s.txt"],
      "sources": ["src/s.txt"],
      "outputs": ["out/base.txt"]
    },
    {
      "id": "child",
      "tool": "tools/concat.sh",
      "args": ["out/child.txt", "in/up/out/base.txt"],
      "upstream": {"up": "base"},
      "outputs": ["out/child.txt"]
    }
  ]
}
`

// InstallTwoNodeProject creates base -> child.
func InstallTwoNodeProject(t *testing.T, st *store.Store) *spec.Project {
	t.Helper()
	return InstallProject(t, st, "two-demo", TwoNodeBuildJSON, map[string]string{
		"src/s.txt": "base source\n",
	})
}

// Overwrite rewrites a file inside a project tree (tests only).
func Overwrite(t *testing.T, path, content string) {
	t.Helper()
	if err := os.Chmod(path, 0o644); err != nil && !os.IsNotExist(err) {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

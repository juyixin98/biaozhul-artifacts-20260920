package store_test

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	"github.example.com/ocimultipick/internal/store"
)

func openStore(t *testing.T) *store.Store {
	t.Helper()
	ctx := context.Background()
	s, err := store.Open(ctx, filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func TestTagUpsertAndMove(t *testing.T) {
	ctx := context.Background()
	s := openStore(t)

	first := store.Tag{Repository: "repo/x", Tag: "latest", Digest: "sha256:aaa", MediaType: "application/vnd.oci.image.index.v1+json"}
	if err := s.UpsertTag(ctx, first); err != nil {
		t.Fatal(err)
	}
	got, err := s.GetTag(ctx, "repo/x", "latest")
	if err != nil {
		t.Fatal(err)
	}
	if got.Digest != "sha256:aaa" {
		t.Fatalf("initial tag digest = %s", got.Digest)
	}

	// Move the tag: same name now points at a different artifact.
	moved := first
	moved.Digest = "sha256:bbb"
	if err := s.UpsertTag(ctx, moved); err != nil {
		t.Fatal(err)
	}
	got, _ = s.GetTag(ctx, "repo/x", "latest")
	if got.Digest != "sha256:bbb" {
		t.Fatalf("moved tag digest = %s, want bbb", got.Digest)
	}
}

func TestSameTagNameDifferentRepoIsDifferentArtifact(t *testing.T) {
	ctx := context.Background()
	s := openStore(t)
	_ = s.UpsertTag(ctx, store.Tag{Repository: "repo/one", Tag: "latest", Digest: "sha256:111"})
	_ = s.UpsertTag(ctx, store.Tag{Repository: "repo/two", Tag: "latest", Digest: "sha256:222"})

	a, err := s.GetTag(ctx, "repo/one", "latest")
	if err != nil {
		t.Fatal(err)
	}
	b, err := s.GetTag(ctx, "repo/two", "latest")
	if err != nil {
		t.Fatal(err)
	}
	if a.Digest == b.Digest {
		t.Fatalf("identical tag names in different repos must not be treated as the same artifact: both %s", a.Digest)
	}
}

func TestTaskPersistsFullChain(t *testing.T) {
	ctx := context.Background()
	s := openStore(t)
	task := &store.Task{
		ID:             "task-1",
		Repository:     "repo/x",
		Reference:      "latest",
		ResolvedTag:    "sha256:idx",
		RootDigest:     "sha256:idx",
		OS:             "linux",
		Architecture:   "amd64",
		Status:         "succeeded",
		ManifestDigest: "sha256:man",
		Chain: []store.ChainEntry{
			{Position: 0, Role: "index", Digest: "sha256:idx", Size: 100},
			{Position: 1, Role: "manifest", Digest: "sha256:man", Size: 200},
			{Position: 2, Role: "config", Digest: "sha256:cfg", Size: 50},
			{Position: 3, Role: "layer", Digest: "sha256:lay", Size: 999},
		},
	}
	if err := s.CreateTask(ctx, task); err != nil {
		t.Fatal(err)
	}
	back, err := s.GetTask(ctx, "task-1")
	if err != nil {
		t.Fatal(err)
	}
	if len(back.Chain) != 4 {
		t.Fatalf("chain length = %d, want 4", len(back.Chain))
	}
	if back.Chain[3].Digest != "sha256:lay" || back.Chain[3].Size != 999 {
		t.Fatalf("last chain entry wrong: %+v", back.Chain[3])
	}
	if back.Chain[0].Role != "index" {
		t.Fatalf("chain ordering lost: %+v", back.Chain)
	}
}

func TestFailedTaskRecorded(t *testing.T) {
	ctx := context.Background()
	s := openStore(t)
	task := &store.Task{
		ID: "task-fail", Repository: "repo/x", Reference: "latest", RootDigest: "sha256:idx",
		OS: "linux", Architecture: "amd64", Status: "failed",
		ErrorCode: "blob_missing", ErrorDetail: "layer absent",
	}
	if err := s.CreateTask(ctx, task); err != nil {
		t.Fatal(err)
	}
	back, err := s.GetTask(ctx, "task-fail")
	if err != nil {
		t.Fatal(err)
	}
	if back.Status != "failed" || back.ErrorCode != "blob_missing" {
		t.Fatalf("failed task not preserved: %+v", back)
	}
}

func TestGetMissingTask(t *testing.T) {
	s := openStore(t)
	if _, err := s.GetTask(context.Background(), "nope"); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
}

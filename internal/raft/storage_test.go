package raft

import (
	"path/filepath"
	"testing"
)

func TestFileStorageRoundTrip(t *testing.T) {
	dir := t.TempDir()
	s := NewFileStorage(dir)
	if _, err := s.Load(); err != ErrEmptyStorage {
		t.Fatalf("fresh storage err=%v, want ErrEmptyStorage", err)
	}
	want := PersistentState{
		CurrentTerm:  7,
		VotedFor:     3,
		Log:          []Entry{{Term: 1, Command: "a"}, {Term: 7, Command: "b"}},
		CommitIndex:  1,
		AppliedIndex: 1,
	}
	if err := s.Save(want); err != nil {
		t.Fatal(err)
	}
	got, err := NewFileStorage(dir).Load()
	if err != nil {
		t.Fatal(err)
	}
	if got.CurrentTerm != want.CurrentTerm || got.VotedFor != want.VotedFor ||
		got.CommitIndex != want.CommitIndex || got.AppliedIndex != want.AppliedIndex ||
		len(got.Log) != 2 || got.Log[1].Command != "b" {
		t.Fatalf("round trip mismatch: %+v", got)
	}
	if filepath.Base(s.path) != "raft-state.json" {
		t.Fatalf("unexpected storage path %s", s.path)
	}
}

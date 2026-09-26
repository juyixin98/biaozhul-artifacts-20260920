package main

import (
	"context"
	"testing"
)

func TestSeededArtifacts(t *testing.T) {
	reg := newRegistry()
	ctx := context.Background()

	want := map[string]int{
		"binary256": 256,
		"lorem300":  300,
		"empty":     0,
	}
	for id, size := range want {
		a, err := reg.Get(ctx, id)
		if err != nil {
			t.Fatalf("artifact %s: %v", id, err)
		}
		rep, err := a.Representation("identity")
		if err != nil {
			t.Fatalf("representation %s: %v", id, err)
		}
		if len(rep) != size {
			t.Errorf("%s size = %d, want %d", id, len(rep), size)
		}
		if a.ETag() == "" || a.ETag()[0] != '"' {
			t.Errorf("%s etag = %q", id, a.ETag())
		}
	}
	if len(reg.IDs()) != len(want) {
		t.Fatalf("ids = %v", reg.IDs())
	}
}

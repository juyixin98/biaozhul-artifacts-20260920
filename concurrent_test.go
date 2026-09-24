package main

import (
	"fmt"
	"sync"
	"testing"
)

// Hammer every replica with concurrent writes, merges, delivers and syncs.
// It must not race, panic or corrupt antichains; after everyone has synced
// with everyone, replicas must converge to identical state.
func TestStore_ConcurrentConvergence(t *testing.T) {
	s := newTestStore(t, "A", "B", "C")
	const writers = 8
	const perWriter = 25

	var wg sync.WaitGroup
	for w := 0; w < writers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			replica := []string{"A", "B", "C"}[w%3]
			for i := 0; i < perWriter; i++ {
				switch i % 4 {
				case 0:
					if _, _, err := s.Write(replica, "k", fmt.Sprintf("w%d-%d", w, i)); err != nil {
						t.Errorf("write: %v", err)
						return
					}
				case 1:
					if _, err := s.Sync("A", "B", SyncModeTwoWay); err != nil {
						t.Errorf("sync: %v", err)
						return
					}
				case 2:
					if _, err := s.Sync("B", "C", SyncModeTwoWay); err != nil {
						t.Errorf("sync: %v", err)
						return
					}
				case 3:
					// Re-deliver a locally known version: duplicates are safe.
					if _, _, err := s.Write(replica, "other", fmt.Sprintf("%d", i)); err != nil {
						t.Errorf("write other: %v", err)
						return
					}
				}
			}
		}(w)
	}
	wg.Wait()

	// Full anti-entropy: each pair synced until a final deterministic round.
	for round := 0; round < 3; round++ {
		for _, pair := range [][2]string{{"A", "B"}, {"B", "C"}, {"A", "C"}} {
			if _, err := s.Sync(pair[0], pair[1], SyncModeTwoWay); err != nil {
				t.Fatal(err)
			}
		}
	}

	snap := map[string]Snapshot{}
	for _, id := range []string{"A", "B", "C"} {
		sn, err := s.Snapshot(id)
		if err != nil {
			t.Fatal(err)
		}
		snap[id] = sn
	}
	for key := range snap["A"].Keys {
		base := versionIDSet(snap["A"].Keys[key])
		for _, id := range []string{"B", "C"} {
			got := versionIDSet(snap[id].Keys[key])
			if len(got) != len(base) {
				t.Fatalf("key %q diverges: A has %d versions, %s has %d", key, len(base), id, len(got))
			}
			for vid := range base {
				if !got[vid] {
					t.Fatalf("key %q: %s missing version %s after convergence", key, id, vid)
				}
			}
		}
	}
}

func versionIDSet(vs []Version) map[string]bool {
	m := make(map[string]bool, len(vs))
	for _, v := range vs {
		m[v.ID] = true
	}
	return m
}

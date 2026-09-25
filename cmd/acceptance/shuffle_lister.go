package main

import (
	"math/rand"
	"os"
)

// seededLister returns directory entries in deterministic pseudo-random
// order so the acceptance harness proves archive bytes are independent of
// readdir order.
type seededLister struct{ rng *rand.Rand }

func newSeededLister(seed int64) *seededLister {
	return &seededLister{rng: rand.New(rand.NewSource(seed))}
}

func (s *seededLister) ReadDir(name string) ([]os.DirEntry, error) {
	des, err := os.ReadDir(name)
	if err != nil {
		return nil, err
	}
	out := make([]os.DirEntry, len(des))
	copy(out, des)
	s.rng.Shuffle(len(out), func(i, j int) { out[i], out[j] = out[j], out[i] })
	return out, nil
}

func (s *seededLister) Lstat(name string) (os.FileInfo, error) {
	return os.Lstat(name)
}

package main

import (
	"math/rand"
	"os"
)

// shuffledLister randomizes the order of ReadDir results while delegating
// everything else to the os package. It exists to demonstrate that the
// archive output is independent of directory traversal order.
type shuffledLister struct{ rng *rand.Rand }

func newShuffledLister(seed int64) *shuffledLister {
	return &shuffledLister{rng: rand.New(rand.NewSource(seed))}
}

func (s *shuffledLister) ReadDir(name string) ([]os.DirEntry, error) {
	des, err := os.ReadDir(name)
	if err != nil {
		return nil, err
	}
	out := make([]os.DirEntry, len(des))
	copy(out, des)
	s.rng.Shuffle(len(out), func(i, j int) { out[i], out[j] = out[j], out[i] })
	return out, nil
}

func (s *shuffledLister) Lstat(name string) (os.FileInfo, error) {
	return os.Lstat(name)
}

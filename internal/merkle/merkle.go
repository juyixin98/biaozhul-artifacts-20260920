// Package merkle builds fixed-partition Merkle trees over a key/value
// snapshot.
//
// The key space is split into BucketCount (16) fixed buckets by key hash.
// Each bucket is a leaf whose digest commits to every record in it (value,
// version and tombstone flag all enter the hash, so any change — including a
// delete or a version-only bump — flips the leaf). Internal levels are a
// complete binary tree of hashes: a node hash commits to its two children,
// so two replicas can compare from the root and descend only into subtrees
// whose digests differ.
package merkle

import (
	"crypto/sha256"
	"encoding/hex"
	"hash/fnv"
	"sort"
	"strconv"

	"merklesync/internal/store"
)

const (
	BucketCount = 16
	// Level 0 is the leaf level (16 nodes), then 8 / 4 / 2 / 1 (root).
	Levels    = 5
	RootLevel = Levels - 1
)

// Tree is an immutable snapshot tree.
type Tree struct {
	// nodes[level][index], level 0 = leaves (BucketCount entries),
	// last level = one root.
	nodes [][]string
	// leafEntries[bucket] holds the entries that went into that leaf,
	// sorted by key. Useful for local diffing without re-snapshotting.
	leafEntries [][]store.Entry
}

// HashKey assigns a key to a fixed bucket.
func HashKey(key string) int {
	h := fnv.New32a()
	h.Write([]byte(key))
	return int(h.Sum32() % BucketCount)
}

func hashLeaf(entries []store.Entry) string {
	h := sha256.New()
	h.Write([]byte("leaf-v1\n"))
	for _, e := range entries {
		h.Write([]byte(e.Key))
		h.Write([]byte{0})
		h.Write([]byte(e.Value))
		h.Write([]byte{0})
		h.Write([]byte(strconv.FormatInt(e.Version, 10)))
		h.Write([]byte{0})
		if e.Deleted {
			h.Write([]byte("D"))
		} else {
			h.Write([]byte("L"))
		}
		h.Write([]byte{'\n'})
	}
	return hex.EncodeToString(h.Sum(nil))
}

func hashInner(left, right string) string {
	h := sha256.New()
	h.Write([]byte("node-v1\n"))
	h.Write([]byte(left))
	h.Write([]byte{'\n'})
	h.Write([]byte(right))
	return hex.EncodeToString(h.Sum(nil))
}

// Build constructs a tree from a snapshot. Entries are bucketed by HashKey;
// within a bucket they are sorted by key so the digest is order-independent.
func Build(snap store.Snapshot) *Tree {
	buckets := make([][]store.Entry, BucketCount)
	for _, e := range snap.Entries {
		b := HashKey(e.Key)
		buckets[b] = append(buckets[b], e)
	}
	t := &Tree{leafEntries: buckets}
	for b := range buckets {
		sort.Slice(buckets[b], func(i, j int) bool {
			return buckets[b][i].Key < buckets[b][j].Key
		})
	}
	t.nodes = make([][]string, Levels)
	t.nodes[0] = make([]string, BucketCount)
	for b := 0; b < BucketCount; b++ {
		t.nodes[0][b] = hashLeaf(buckets[b])
	}
	width := BucketCount
	for level := 1; level < Levels; level++ {
		width /= 2
		t.nodes[level] = make([]string, width)
		for i := 0; i < width; i++ {
			t.nodes[level][i] = hashInner(t.nodes[level-1][2*i], t.nodes[level-1][2*i+1])
		}
	}
	return t
}

// Root returns the root digest (level 4, index 0).
func (t *Tree) Root() string { return t.nodes[RootLevel][0] }

// Node returns the digest at a level/index, or "" if out of range.
func (t *Tree) Node(level, index int) string {
	if level < 0 || level >= len(t.nodes) || index < 0 || index >= len(t.nodes[level]) {
		return ""
	}
	return t.nodes[level][index]
}

// LeafHash returns a bucket leaf digest.
func (t *Tree) LeafHash(bucket int) string { return t.Node(0, bucket) }

// LeafEntries returns the entries that were hashed into a bucket (sorted).
func (t *Tree) LeafEntries(bucket int) []store.Entry {
	if bucket < 0 || bucket >= BucketCount {
		return nil
	}
	return t.leafEntries[bucket]
}

// Width returns the number of nodes at a level.
func Width(level int) int {
	if level < 0 || level >= Levels {
		return 0
	}
	return BucketCount >> level
}

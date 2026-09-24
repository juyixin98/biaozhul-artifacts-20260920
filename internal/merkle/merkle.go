// Package merkle builds fixed-partition Merkle trees over store snapshots.
//
// The key space is split into a fixed number of buckets N = fanout^depth,
// assigned by FNV-1a(key) mod N. Buckets are the leaves of a complete
// fanout-ary tree of the given depth; internal node hashes commit to the
// concatenation of their children's hashes. Because the partition is fixed
// and content-addressed, two replicas with the same fanout/depth can
// compare trees top-down: equal subtrees are skipped, only hashes along
// paths to differing buckets are exchanged, and only entries inside those
// buckets ever travel over the wire. Empty subtrees hash identically, so
// sparse key sets are cheap.
package merkle

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"hash/fnv"
	"strconv"
	"strings"

	"github.com/example/merklekv/internal/hlc"
	"github.com/example/merklekv/internal/store"
)

const (
	leafDomain = "merklekv:leaf-v1"
	nodeDomain = "merklekv:node-v1"
)

// Params describes the fixed partitioning.
type Params struct {
	Fanout int
	Depth  int
}

// NewParams validates the partitioning. Depth is capped at 10 to keep
// fanout^depth within int range for fanout 16.
func NewParams(fanout, depth int) (Params, error) {
	if fanout < 2 || fanout > 256 {
		return Params{}, fmt.Errorf("merkle: fanout must be in [2,256], got %d", fanout)
	}
	if depth < 1 || depth > 10 {
		return Params{}, fmt.Errorf("merkle: depth must be in [1,10], got %d", depth)
	}
	return Params{Fanout: fanout, Depth: depth}, nil
}

// NumBuckets returns fanout^depth.
func (p Params) NumBuckets() int {
	n := 1
	for i := 0; i < p.Depth; i++ {
		n *= p.Fanout
	}
	return n
}

// BucketIndex assigns a key to a bucket.
func (p Params) BucketIndex(key string) int {
	h := fnv.New64a()
	h.Write([]byte(key))
	return int(h.Sum64() % uint64(p.NumBuckets()))
}

// Tree is a Merkle tree built over one immutable snapshot.
type Tree struct {
	params Params
	// buckets holds snapshot entries grouped by bucket; entries inside a
	// bucket stay in snapshot (key-sorted) order.
	buckets    [][]store.Entry
	leafHashes []string
	// levels[0] is the single root node; levels[depth] are the leaf nodes,
	// one per bucket, in bucket order.
	levels [][]string
}

// Build constructs the tree for the given snapshot.
func Build(p Params, snap *store.Snapshot) *Tree {
	n := p.NumBuckets()
	t := &Tree{
		params:     p,
		buckets:    make([][]store.Entry, n),
		leafHashes: make([]string, n),
	}
	for _, e := range snap.Entries() {
		idx := p.BucketIndex(e.Key)
		t.buckets[idx] = append(t.buckets[idx], e)
	}
	leaves := make([]string, n)
	for i := 0; i < n; i++ {
		h := hashLeaf(t.buckets[i])
		leaves[i] = h
		t.leafHashes[i] = h
	}
	// Allocate root-first: levels[depth] = leaves, then fill bottom-up.
	t.levels = make([][]string, p.Depth+1)
	t.levels[p.Depth] = leaves
	current := leaves
	for level := p.Depth - 1; level >= 0; level-- {
		next := make([]string, len(current)/p.Fanout)
		for i := range next {
			from, to := i*p.Fanout, (i+1)*p.Fanout
			next[i] = hashInterior(level+1, current[from:to])
		}
		t.levels[level] = next
		current = next
	}
	return t
}

// Params returns the tree parameters.
func (t *Tree) Params() Params { return t.params }

// RootHash is the hex hash of the root node.
func (t *Tree) RootHash() string { return t.levels[0][0] }

// BucketEntries returns the entries assigned to one bucket (key order).
func (t *Tree) BucketEntries(idx int) []store.Entry {
	if idx < 0 || idx >= t.params.NumBuckets() {
		return nil
	}
	return t.buckets[idx]
}

// Node describes a tree node on the wire.
type Node struct {
	Path     string   `json:"path"`
	Level    int      `json:"level"`
	Leaf     bool     `json:"leaf"`
	Hash     string   `json:"hash"`
	Children []string `json:"children,omitempty"` // child hashes, fanout long
	// Bucket metadata, present only when Leaf is true.
	Bucket int `json:"bucket,omitempty"`
	Count  int `json:"count,omitempty"`
}

// ParsePath converts a "1/7/3" path into digits, validating bounds.
func (p Params) ParsePath(path string) ([]int, error) {
	if path == "" || path == "/" {
		return nil, nil
	}
	parts := strings.Split(strings.Trim(path, "/"), "/")
	if len(parts) > p.Depth {
		return nil, fmt.Errorf("merkle: path %q deeper than depth %d", path, p.Depth)
	}
	digits := make([]int, len(parts))
	for i, part := range parts {
		d, err := strconv.Atoi(part)
		if err != nil || d < 0 || d >= p.Fanout {
			return nil, fmt.Errorf("merkle: bad path component %q (fanout=%d)", part, p.Fanout)
		}
		digits[i] = d
	}
	return digits, nil
}

// JoinPath renders digits as "1/7/3"; no digits yields "".
func JoinPath(digits []int) string {
	parts := make([]string, len(digits))
	for i, d := range digits {
		parts[i] = strconv.Itoa(d)
	}
	return strings.Join(parts, "/")
}

// BucketPath returns the leaf path digits for a bucket index.
func (p Params) BucketPath(idx int) []int {
	digits := make([]int, p.Depth)
	div := p.NumBuckets()
	for i := 0; i < p.Depth; i++ {
		div /= p.Fanout
		digits[i] = idx / div
		idx %= div
	}
	return digits
}

// Node returns the node at a path. Leaf nodes carry bucket metadata.
func (t *Tree) Node(path string) (Node, error) {
	digits, err := t.params.ParsePath(path)
	if err != nil {
		return Node{}, err
	}
	level := len(digits)
	if level > t.params.Depth {
		return Node{}, fmt.Errorf("merkle: no node at %q", path)
	}
	// Index of this node within its level.
	idx := 0
	for _, d := range digits {
		idx = idx*t.params.Fanout + d
	}
	hash := t.levels[level][idx]
	node := Node{Path: JoinPath(digits), Level: level, Hash: hash}
	if level == t.params.Depth {
		node.Leaf = true
		node.Bucket = idx
		node.Count = len(t.buckets[idx])
		return node, nil
	}
	from := idx * t.params.Fanout
	node.Children = append([]string(nil), t.levels[level+1][from:from+t.params.Fanout]...)
	return node, nil
}

// LeafHash returns the leaf hash for a bucket index.
func (t *Tree) LeafHash(idx int) string { return t.leafHashes[idx] }

// ExpectedRoot recomputes the root hash when specific leaves are replaced.
//
// leaves maps bucket index -> new leaf hash. Only buckets present in the map
// change; everything else is taken from this tree. This lets a sync client
// compute the post-merge root it expects both replicas to converge to,
// before sending any data.
func (t *Tree) ExpectedRoot(leaves map[int]string) (string, error) {
	n := t.params.NumBuckets()
	current := make([]string, n)
	copy(current, t.leafHashes)
	for idx, h := range leaves {
		if idx < 0 || idx >= n {
			return "", fmt.Errorf("merkle: bucket %d out of range [0,%d)", idx, n)
		}
		current[idx] = h
	}
	// Combine bottom-up, matching Build's domain labels: the node produced
	// by hashing level L children is labeled L (so the root is labeled 1).
	for childLevel := t.params.Depth; childLevel >= 1; childLevel-- {
		next := make([]string, len(current)/t.params.Fanout)
		for i := range next {
			from, to := i*t.params.Fanout, (i+1)*t.params.Fanout
			next[i] = hashInterior(childLevel, current[from:to])
		}
		current = next
	}
	return current[0], nil
}

// HashLeaf is exported so sync code can hash merged bucket contents.
func HashLeaf(entries []store.Entry) string { return hashLeaf(entries) }

// hashLeaf hashes the full versioned state of one bucket. Tombstones,
// versions and origins are all included, so resurrecting, updating or
// deleting a key all change the hash. Entries must be key-sorted
// (snapshots are, and merged buckets are re-sorted by the caller).
func hashLeaf(entries []store.Entry) string {
	h := sha256.New()
	h.Write([]byte(leafDomain))
	h.Write([]byte{0})
	var buf [8]byte
	n := uint64(len(entries))
	binary.BigEndian.PutUint64(buf[:], n)
	h.Write(buf[:])
	for _, e := range entries {
		writeLenBytes(h, []byte(e.Key))
		writeLenBytes(h, e.Value)
		if e.Deleted {
			h.Write([]byte{1})
		} else {
			h.Write([]byte{0})
		}
		binary.BigEndian.PutUint64(buf[:], e.Ver.Wall)
		h.Write(buf[:])
		binary.BigEndian.PutUint64(buf[:], e.Ver.Log)
		h.Write(buf[:])
		writeLenBytes(h, []byte(e.Origin))
	}
	return hex.EncodeToString(h.Sum(nil))
}

func writeLenBytes(h interface{ Write([]byte) (int, error) }, b []byte) {
	var buf [8]byte
	binary.BigEndian.PutUint64(buf[:], uint64(len(b)))
	h.Write(buf[:])
	h.Write(b)
}

// hashInterior binds the level into the domain separation, so identical
// hashes at different depths cannot collide.
func hashInterior(level int, children []string) string {
	h := sha256.New()
	h.Write([]byte(nodeDomain))
	h.Write([]byte{0})
	h.Write([]byte{byte(level)})
	for _, c := range children {
		raw, err := hex.DecodeString(c)
		if err != nil {
			panic("merkle: internal corruption: " + err.Error())
		}
		h.Write(raw)
	}
	return hex.EncodeToString(h.Sum(nil))
}

// Older reports whether version a is older than b; exported helper for
// merge code that wants to be explicit about comparisons.
func Older(a, b hlc.Timestamp) bool { return a.Compare(b) < 0 }

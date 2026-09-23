// Package store is the Pebble-backed durable log of blocks and per-block
// state deltas, plus small chain metadata records.
//
// Key layout (big-endian height keeps chronological range iteration):
//
//	"b" || height(8)            -> JSON block (header + transactions)
//	"d" || height(8)            -> JSON delta
//	"m" || name                 -> metadata (tip, genesis...)
package store

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/cockroachdb/pebble"
	"github.com/example/snapshotprune/internal/types"
)

// ErrNotFound is returned for missing keys.
var ErrNotFound = errors.New("record not found")

const (
	prefixBlock = 'b'
	prefixDelta = 'd'
	prefixMeta  = 'm'
)

// Store wraps a Pebble database.
type Store struct {
	db *pebble.DB
}

// Open opens (or creates) the Pebble database at dir.
func Open(dir string) (*Store, error) {
	db, err := pebble.Open(dir, &pebble.Options{})
	if err != nil {
		return nil, fmt.Errorf("open pebble at %s: %w", dir, err)
	}
	return &Store{db: db}, nil
}

// Close flushes and closes the database.
func (s *Store) Close() error { return s.db.Close() }

// DB exposes the raw database for snapshot-iterator use.
func (s *Store) DB() *pebble.DB { return s.db }

func blockKey(h uint64) []byte {
	k := make([]byte, 9)
	k[0] = prefixBlock
	binary.BigEndian.PutUint64(k[1:], h)
	return k
}

func deltaKey(h uint64) []byte {
	k := make([]byte, 9)
	k[0] = prefixDelta
	binary.BigEndian.PutUint64(k[1:], h)
	return k
}

func metaKey(name string) []byte { return append([]byte{prefixMeta}, []byte(name)...) }

func (s *Store) get(key []byte) ([]byte, error) {
	v, closer, err := s.db.Get(key)
	if errors.Is(err, pebble.ErrNotFound) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	out := make([]byte, len(v))
	copy(out, v)
	closer.Close()
	return out, nil
}

// PutBlockAndDelta commits a block and the delta of the same height, with
// the chain tip pointer, as one atomic batch.
func (s *Store) PutBlockAndDelta(b *types.Block, d *types.Delta) error {
	blockData, err := json.Marshal(b)
	if err != nil {
		return err
	}
	deltaData, err := json.Marshal(d)
	if err != nil {
		return err
	}
	var tip [8]byte
	binary.BigEndian.PutUint64(tip[:], b.Header.Height)

	batch := s.db.NewBatch()
	defer batch.Close()
	if err := batch.Set(blockKey(b.Header.Height), blockData, nil); err != nil {
		return err
	}
	if err := batch.Set(deltaKey(b.Header.Height), deltaData, nil); err != nil {
		return err
	}
	if err := batch.Set(metaKey("tip"), tip[:], nil); err != nil {
		return err
	}
	return batch.Commit(pebble.Sync)
}

// PutGenesis bootstraps an empty chain: stores the genesis block (height 0),
// its empty delta and the tip pointer atomically.
func (s *Store) PutGenesis(b *types.Block) error {
	blockData, err := json.Marshal(b)
	if err != nil {
		return err
	}
	deltaData, err := json.Marshal(&types.Delta{Height: 0})
	if err != nil {
		return err
	}
	var tip [8]byte
	batch := s.db.NewBatch()
	defer batch.Close()
	if err := batch.Set(blockKey(0), blockData, nil); err != nil {
		return err
	}
	if err := batch.Set(deltaKey(0), deltaData, nil); err != nil {
		return err
	}
	if err := batch.Set(metaKey("tip"), tip[:], nil); err != nil {
		return err
	}
	return batch.Commit(pebble.Sync)
}

// SetMeta stores an opaque metadata record.
func (s *Store) SetMeta(name string, value []byte) error {
	return s.db.Set(metaKey(name), value, pebble.Sync)
}

// GetMeta reads an opaque metadata record.
func (s *Store) GetMeta(name string) ([]byte, error) {
	return s.get(metaKey(name))
}

// Tip returns the current chain tip height.
func (s *Store) Tip() (uint64, error) {
	v, err := s.get(metaKey("tip"))
	if err != nil {
		return 0, err
	}
	return binary.BigEndian.Uint64(v), nil
}

// Block returns the block at height.
func (s *Store) Block(h uint64) (*types.Block, error) {
	v, err := s.get(blockKey(h))
	if err != nil {
		return nil, err
	}
	var b types.Block
	if err := json.Unmarshal(v, &b); err != nil {
		return nil, err
	}
	return &b, nil
}

// Delta returns the delta at height.
func (s *Store) Delta(h uint64) (*types.Delta, error) {
	v, err := s.get(deltaKey(h))
	if err != nil {
		return nil, err
	}
	var d types.Delta
	if err := json.Unmarshal(v, &d); err != nil {
		return nil, err
	}
	return &d, nil
}

// HasDelta reports whether the delta at height is still present.
func (s *Store) HasDelta(h uint64) (bool, error) {
	_, closer, err := s.db.Get(deltaKey(h))
	if errors.Is(err, pebble.ErrNotFound) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	closer.Close()
	return true, nil
}

// PruneDeltas deletes delta records at heights in [1, belowOrEqual].
// Height 0 (genesis) is never deleted. Returns the number of records removed.
func (s *Store) PruneDeltas(belowOrEqual uint64) (int, error) {
	if belowOrEqual < 1 {
		return 0, nil
	}
	batch := s.db.NewBatch()
	defer batch.Close()
	n := 0
	for h := uint64(1); h <= belowOrEqual; h++ {
		if err := batch.Delete(deltaKey(h), nil); err != nil {
			return 0, err
		}
		n++
	}
	if err := batch.Commit(pebble.Sync); err != nil {
		return 0, err
	}
	return n, nil
}

// LowestDeltaHeight returns the smallest height whose delta still exists,
// or (0, false) if only genesis (height 0) remains. Scans the delta key
// range; cheap relative to snapshot construction frequency.
func (s *Store) LowestDeltaHeight() (uint64, bool, error) {
	lo := deltaKey(0)
	hi := append([]byte{prefixDelta}, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff)
	iter, err := s.db.NewIter(&pebble.IterOptions{LowerBound: lo, UpperBound: hi})
	if err != nil {
		return 0, false, err
	}
	defer iter.Close()
	if !iter.First() {
		return 0, false, nil
	}
	k := iter.Key()
	if len(k) != 9 {
		return 0, false, fmt.Errorf("corrupt delta key length %d", len(k))
	}
	h := binary.BigEndian.Uint64(k[1:])
	return h, true, nil
}

// Reader is the read-only view interface used by replay and snapshot build
// so they can operate either on live state or on a Pebble snapshot.
type Reader interface {
	Block(h uint64) (*types.Block, error)
	Delta(h uint64) (*types.Delta, error)
	Tip() (uint64, error)
}

type pebbleReader struct {
	r pebble.Reader
}

func (pr *pebbleReader) valueAt(key []byte) ([]byte, error) {
	v, closer, err := pr.r.Get(key)
	if errors.Is(err, pebble.ErrNotFound) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	out := make([]byte, len(v))
	copy(out, v)
	closer.Close()
	return out, nil
}

func (pr *pebbleReader) Block(h uint64) (*types.Block, error) {
	v, err := pr.valueAt(blockKey(h))
	if err != nil {
		return nil, err
	}
	var b types.Block
	if err := json.Unmarshal(v, &b); err != nil {
		return nil, err
	}
	return &b, nil
}

func (pr *pebbleReader) Delta(h uint64) (*types.Delta, error) {
	v, err := pr.valueAt(deltaKey(h))
	if err != nil {
		return nil, err
	}
	var d types.Delta
	if err := json.Unmarshal(v, &d); err != nil {
		return nil, err
	}
	return &d, nil
}

func (pr *pebbleReader) Tip() (uint64, error) {
	v, err := pr.valueAt(metaKey("tip"))
	if err != nil {
		return 0, err
	}
	return binary.BigEndian.Uint64(v), nil
}

// NewSnapshotReader returns a consistent point-in-time reader backed by a
// Pebble snapshot. The returned closer must be called when done.
func (s *Store) NewSnapshotReader() (Reader, func(), error) {
	snap := s.db.NewSnapshot()
	r := &pebbleReader{r: snap}
	return r, func() { snap.Close() }, nil
}

// LiveReader returns a reader backed by the live database.
func (s *Store) LiveReader() Reader {
	return &pebbleReader{r: s.db}
}

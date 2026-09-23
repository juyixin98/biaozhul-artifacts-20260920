// Package store implements a versioned account-state store on top of Pebble.
//
// Layout inside the Pebble database:
//
//	meta/head                  -> 8-byte big-endian current head height
//	delta/<8-byte BE height>   -> JSON Delta{Height, Changes}   (one per block)
//	snap/manifest              -> JSON Manifest, only *committed* snapshots
//	snap/seal/<height>         -> JSON SnapshotMeta, written in the same batch
//	                              as the manifest update (atomic commit point)
//	snap/data/<height>/<acct>  -> decimal balance, one key per account
//
// A snapshot under construction has snap/data keys but no manifest entry and
// no seal; a crash mid-build therefore leaves an *orphan* that recovery
// deletes on the next Open and that is never listed as available.
package store

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/cockroachdb/pebble"
)

const (
	keyMetaHead = "meta/head"
	keyManifest = "snap/manifest"
)

var (
	// ErrHeightInFuture is returned when querying past the committed head.
	ErrHeightInFuture = errors.New("requested height is beyond the committed head")
	// ErrHeightPruned is returned when the deltas needed to serve a height
	// have already been pruned.
	ErrHeightPruned = errors.New("requested height has been pruned")
	// ErrReaderTimeout is returned when a reader's lease expires before the
	// query finishes. Readers always fail explicitly, never silently.
	ErrReaderTimeout = errors.New("reader lease expired")
	// ErrInvalidBlock is returned for malformed block input.
	ErrInvalidBlock = errors.New("invalid block")
)

// Delta is the per-block increment: balance changes keyed by account.
type Delta struct {
	Height  uint64           `json:"height"`
	Changes map[string]int64 `json:"changes"`
}

// Summary is the state digest returned for a historical query.
type Summary struct {
	Height       uint64 `json:"height"`
	BaseSnapshot uint64 `json:"base_snapshot"`
	Accounts     int    `json:"accounts"`
	TotalBalance int64  `json:"total_balance"`
	StateHash    string `json:"state_hash"`
}

// SnapshotMeta describes one committed snapshot.
type SnapshotMeta struct {
	Height       uint64    `json:"height"`
	Accounts     int       `json:"accounts"`
	TotalBalance int64     `json:"total_balance"`
	StateHash    string    `json:"state_hash"`
	CreatedAt    time.Time `json:"created_at"`
}

// Manifest is the durable list of committed snapshots, ascending by height.
type Manifest struct {
	Snapshots []SnapshotMeta `json:"snapshots"`
}

// Options tunes a Store. Zero values get defaults.
type Options struct {
	// KeepSnapshots is how many committed snapshots pruning retains. Default 3.
	KeepSnapshots int
	// ReaderTTL is the default lease duration for historical queries.
	// Default 10s. A query that outlives its lease fails with ErrReaderTimeout.
	ReaderTTL time.Duration
	// SnapshotInterval, if > 0, triggers an asynchronous snapshot build
	// every SnapshotInterval blocks. Default 0 (manual builds only).
	SnapshotInterval uint64
	// Now is the clock; tests override it.
	Now func() time.Time
	// Logger receives recovery/prune diagnostics. Default log.Printf.
	Logger func(format string, args ...any)
	// ApplyHook (test only) runs before each delta is applied during a read.
	ApplyHook func(height uint64)
	// BuildHook (test only) runs before each snapshot data key is written;
	// returning an error aborts the build, simulating a crash mid-build.
	BuildHook func(written int) error
}

// Store is a Pebble-backed versioned account-state store.
type Store struct {
	db   *pebble.DB
	opts Options

	mu       sync.RWMutex // guards head and manifest
	head     uint64
	manifest Manifest

	readers *readerRegistry
}

// Open opens (or creates) the store in dir and runs crash recovery:
// orphaned snapshot data from interrupted builds is removed, and manifest
// entries whose seal or data is missing/corrupt are dropped so they are
// never listed as available.
func Open(dir string, opts Options) (*Store, error) {
	if opts.KeepSnapshots <= 0 {
		opts.KeepSnapshots = 3
	}
	if opts.ReaderTTL <= 0 {
		opts.ReaderTTL = 10 * time.Second
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Logger == nil {
		opts.Logger = log.Printf
	}
	db, err := pebble.Open(dir, &pebble.Options{})
	if err != nil {
		return nil, fmt.Errorf("open pebble: %w", err)
	}
	s := &Store{db: db, opts: opts, readers: newReaderRegistry(opts.Now)}
	if err := s.loadMeta(); err != nil {
		db.Close()
		return nil, err
	}
	if err := s.recover(); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

// Close flushes and closes the underlying database.
func (s *Store) Close() error { return s.db.Close() }

// Head returns the latest committed block height.
func (s *Store) Head() uint64 {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.head
}

func (s *Store) loadMeta() error {
	if v, closer, err := s.db.Get([]byte(keyMetaHead)); err == nil {
		if len(v) != 8 {
			closer.Close()
			return fmt.Errorf("corrupt head key: %d bytes", len(v))
		}
		s.head = binary.BigEndian.Uint64(v)
		closer.Close()
	} else if !errors.Is(err, pebble.ErrNotFound) {
		return err
	}
	if v, closer, err := s.db.Get([]byte(keyManifest)); err == nil {
		if err := json.Unmarshal(v, &s.manifest); err != nil {
			closer.Close()
			return fmt.Errorf("corrupt manifest: %w", err)
		}
		closer.Close()
	} else if !errors.Is(err, pebble.ErrNotFound) {
		return err
	}
	return nil
}

// recover drops incomplete/corrupt snapshots and deletes orphan data.
func (s *Store) recover() error {
	kept := make([]SnapshotMeta, 0, len(s.manifest.Snapshots))
	keptHeight := map[uint64]bool{}
	for _, m := range s.manifest.Snapshots {
		if err := s.verifySnapshot(m); err != nil {
			s.opts.Logger("recovery: dropping snapshot at height %d: %v", m.Height, err)
			s.deleteSnapshotData(m.Height)
			continue
		}
		kept = append(kept, m)
		keptHeight[m.Height] = true
	}
	if len(kept) != len(s.manifest.Snapshots) {
		s.manifest.Snapshots = kept
		if err := s.persistManifestLocked(); err != nil {
			return err
		}
	}
	// Remove orphan snap/data and snap/seal keys not referenced by the
	// manifest — these are temporary snapshots from interrupted builds.
	orphans, err := s.scanSnapshotHeights()
	if err != nil {
		return err
	}
	for _, h := range orphans {
		if keptHeight[h] {
			continue
		}
		s.opts.Logger("recovery: removing temporary snapshot at height %d (never committed)", h)
		s.deleteSnapshotData(h)
	}
	return nil
}

// verifySnapshot checks that the seal exists and that the on-disk data still
// matches the sealed account count, total balance and state hash.
func (s *Store) verifySnapshot(m SnapshotMeta) error {
	v, closer, err := s.db.Get([]byte(snapSealKey(m.Height)))
	if errors.Is(err, pebble.ErrNotFound) {
		return errors.New("seal missing (incomplete snapshot)")
	}
	if err != nil {
		return err
	}
	closer.Close()
	var seal SnapshotMeta
	if err := json.Unmarshal(v, &seal); err != nil {
		return fmt.Errorf("seal corrupt: %w", err)
	}
	state, err := s.loadSnapshotData(context.Background(), m.Height, time.Time{})
	if err != nil {
		return err
	}
	if len(state) != seal.Accounts {
		return fmt.Errorf("account count mismatch: seal %d, data %d", seal.Accounts, len(state))
	}
	if sum := totalBalance(state); sum != seal.TotalBalance {
		return fmt.Errorf("total balance mismatch: seal %d, data %d", seal.TotalBalance, sum)
	}
	if h := hashState(state); h != seal.StateHash {
		return fmt.Errorf("state hash mismatch: seal %s, data %s", seal.StateHash, h)
	}
	return nil
}

// scanSnapshotHeights lists every height that has snap/data or snap/seal keys.
func (s *Store) scanSnapshotHeights() ([]uint64, error) {
	seen := map[uint64]bool{}
	it, err := s.db.NewIter(&pebble.IterOptions{
		LowerBound: []byte("snap/data/"),
		UpperBound: []byte("snap/seam"), // just past "snap/seal/"
	})
	if err != nil {
		return nil, err
	}
	defer it.Close()
	for it.First(); it.Valid(); it.Next() {
		k := string(it.Key())
		if rest, ok := strings.CutPrefix(k, "snap/data/"); ok {
			// snap/data/<height>/<account>
			if i := strings.IndexByte(rest, '/'); i > 0 {
				if v, err := strconv.ParseUint(rest[:i], 10, 64); err == nil {
					seen[v] = true
				}
			}
		} else if rest, ok := strings.CutPrefix(k, "snap/seal/"); ok {
			if v, err := strconv.ParseUint(rest, 10, 64); err == nil {
				seen[v] = true
			}
		}
	}
	if err := it.Error(); err != nil {
		return nil, err
	}
	out := make([]uint64, 0, len(seen))
	for h := range seen {
		out = append(out, h)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out, nil
}

// AppendBlock commits one block of balance changes and returns its height.
// Heights are strictly sequential: the new block is always head+1.
func (s *Store) AppendBlock(changes map[string]int64) (uint64, error) {
	if len(changes) == 0 {
		return 0, fmt.Errorf("%w: empty change set", ErrInvalidBlock)
	}
	for acct := range changes {
		if acct == "" || strings.ContainsAny(acct, "/\xff") {
			return 0, fmt.Errorf("%w: bad account name %q", ErrInvalidBlock, acct)
		}
	}
	s.mu.Lock()
	h := s.head + 1
	d := Delta{Height: h, Changes: changes}
	data, err := json.Marshal(d)
	if err != nil {
		s.mu.Unlock()
		return 0, err
	}
	b := s.db.NewBatch()
	defer b.Close()
	if err := b.Set(deltaKey(h), data, nil); err != nil {
		s.mu.Unlock()
		return 0, err
	}
	var hb [8]byte
	binary.BigEndian.PutUint64(hb[:], h)
	if err := b.Set([]byte(keyMetaHead), hb[:], nil); err != nil {
		s.mu.Unlock()
		return 0, err
	}
	if err := b.Commit(pebble.Sync); err != nil {
		s.mu.Unlock()
		return 0, err
	}
	s.head = h
	interval := s.opts.SnapshotInterval
	s.mu.Unlock()

	if interval > 0 && h%interval == 0 {
		go func() {
			if _, err := s.BuildSnapshot(context.Background(), h); err != nil {
				s.opts.Logger("auto snapshot at height %d failed: %v", h, err)
			}
		}()
	}
	return h, nil
}

// Summary returns the state digest at height, reconstructing it from the
// newest committed snapshot at or below height plus the deltas above it.
// timeout <= 0 uses the store's ReaderTTL. The query fails explicitly with
// ErrReaderTimeout if its lease expires mid-read.
func (s *Store) Summary(ctx context.Context, height uint64, timeout time.Duration) (Summary, error) {
	state, base, err := s.stateAt(ctx, height, timeout)
	if err != nil {
		return Summary{}, err
	}
	return Summary{
		Height:       height,
		BaseSnapshot: base,
		Accounts:     len(state),
		TotalBalance: totalBalance(state),
		StateHash:    hashState(state),
	}, nil
}

// BalanceAt returns one account's balance at height.
func (s *Store) BalanceAt(ctx context.Context, height uint64, account string, timeout time.Duration) (int64, error) {
	state, _, err := s.stateAt(ctx, height, timeout)
	if err != nil {
		return 0, err
	}
	return state[account], nil
}

// stateAt reconstructs the full account state at height.
func (s *Store) stateAt(ctx context.Context, height uint64, timeout time.Duration) (map[string]int64, uint64, error) {
	if timeout <= 0 {
		timeout = s.opts.ReaderTTL
	}
	s.mu.RLock()
	head := s.head
	base := s.baseSnapshotLocked(height)
	s.mu.RUnlock()
	if height > head {
		return nil, 0, ErrHeightInFuture
	}

	// Register a reader lease so pruning cannot delete the snapshot or the
	// deltas this read depends on while the lease is alive.
	deadline := s.opts.Now().Add(timeout)
	lease := s.readers.acquire(base, deadline)
	defer s.readers.release(lease)

	state := map[string]int64{}
	if base > 0 {
		data, err := s.loadSnapshotData(ctx, base, deadline)
		if err != nil {
			return nil, 0, err
		}
		state = data
	}
	// Apply deltas (base, height].
	it, err := s.db.NewIter(&pebble.IterOptions{
		LowerBound: deltaKey(base + 1),
		UpperBound: deltaKey(height + 1),
	})
	if err != nil {
		return nil, 0, err
	}
	defer it.Close()
	var applied uint64
	for it.First(); it.Valid(); it.Next() {
		if s.opts.Now().After(deadline) {
			return nil, 0, ErrReaderTimeout
		}
		if err := ctx.Err(); err != nil {
			return nil, 0, err
		}
		var d Delta
		if err := json.Unmarshal(it.Value(), &d); err != nil {
			return nil, 0, fmt.Errorf("corrupt delta: %w", err)
		}
		if s.opts.ApplyHook != nil {
			s.opts.ApplyHook(d.Height)
		}
		applyChanges(state, d.Changes)
		applied++
	}
	if err := it.Error(); err != nil {
		return nil, 0, err
	}
	if applied != height-base {
		// The contiguous delta chain is broken: the missing prefix was pruned.
		return nil, 0, ErrHeightPruned
	}
	if s.opts.Now().After(deadline) {
		return nil, 0, ErrReaderTimeout
	}
	return state, base, nil
}

// baseSnapshotLocked returns the highest committed snapshot height <= height,
// or 0 for genesis (no snapshot). Caller must hold s.mu.
func (s *Store) baseSnapshotLocked(height uint64) uint64 {
	base := uint64(0)
	for _, m := range s.manifest.Snapshots {
		if m.Height <= height && m.Height > base {
			base = m.Height
		}
	}
	return base
}

// BuildSnapshot constructs a full snapshot bound to the given fixed height
// (0 means the current head). New blocks keep being written while the build
// runs; the snapshot is committed atomically via a single batch that writes
// the manifest entry and the seal together.
func (s *Store) BuildSnapshot(ctx context.Context, height uint64) (SnapshotMeta, error) {
	s.mu.RLock()
	head := s.head
	s.mu.RUnlock()
	if height == 0 {
		height = head
	}
	if height > head {
		return SnapshotMeta{}, ErrHeightInFuture
	}
	if height == 0 {
		return SnapshotMeta{}, errors.New("nothing to snapshot: no blocks committed")
	}
	s.mu.RLock()
	for _, m := range s.manifest.Snapshots {
		if m.Height == height {
			s.mu.RUnlock()
			return m, nil // already built
		}
	}
	s.mu.RUnlock()

	// Reconstruct state at the bound height. stateAt registers a reader
	// lease, so a concurrent prune cannot pull the deltas from under us.
	state, _, err := s.stateAt(ctx, height, 0)
	if err != nil {
		return SnapshotMeta{}, fmt.Errorf("build snapshot at %d: %w", height, err)
	}

	// Write the snapshot data keys. Until the manifest+seal batch commits,
	// these keys are an invisible temporary snapshot.
	prefix := snapDataPrefix(height)
	accounts := sortedKeys(state)
	batch := s.db.NewBatch()
	written := 0
	flush := func() error {
		if err := batch.Commit(pebble.Sync); err != nil {
			return err
		}
		batch.Close()
		batch = s.db.NewBatch()
		return nil
	}
	for _, acct := range accounts {
		if err := ctx.Err(); err != nil {
			batch.Close()
			return SnapshotMeta{}, err
		}
		if s.opts.BuildHook != nil {
			if err := s.opts.BuildHook(written); err != nil {
				batch.Close()
				return SnapshotMeta{}, fmt.Errorf("build aborted: %w", err)
			}
		}
		if err := batch.Set([]byte(prefix+acct), []byte(strconv.FormatInt(state[acct], 10)), nil); err != nil {
			batch.Close()
			return SnapshotMeta{}, err
		}
		written++
		if written%1000 == 0 {
			if err := flush(); err != nil {
				batch.Close()
				return SnapshotMeta{}, err
			}
		}
	}

	meta := SnapshotMeta{
		Height:       height,
		Accounts:     len(state),
		TotalBalance: totalBalance(state),
		StateHash:    hashState(state),
		CreatedAt:    s.opts.Now().UTC(),
	}

	// Atomic commit point: manifest entry + seal in one batch.
	s.mu.Lock()
	newManifest := Manifest{Snapshots: append(append([]SnapshotMeta{}, s.manifest.Snapshots...), meta)}
	sort.Slice(newManifest.Snapshots, func(i, j int) bool {
		return newManifest.Snapshots[i].Height < newManifest.Snapshots[j].Height
	})
	manData, err := json.Marshal(newManifest)
	if err != nil {
		s.mu.Unlock()
		batch.Close()
		return SnapshotMeta{}, err
	}
	sealData, err := json.Marshal(meta)
	if err != nil {
		s.mu.Unlock()
		batch.Close()
		return SnapshotMeta{}, err
	}
	if err := batch.Set([]byte(keyManifest), manData, nil); err != nil {
		s.mu.Unlock()
		batch.Close()
		return SnapshotMeta{}, err
	}
	if err := batch.Set([]byte(snapSealKey(height)), sealData, nil); err != nil {
		s.mu.Unlock()
		batch.Close()
		return SnapshotMeta{}, err
	}
	if err := batch.Commit(pebble.Sync); err != nil {
		s.mu.Unlock()
		batch.Close()
		return SnapshotMeta{}, err
	}
	batch.Close()
	s.manifest = newManifest
	s.mu.Unlock()

	// Retain only the newest KeepSnapshots snapshots.
	if err := s.Prune(ctx); err != nil {
		s.opts.Logger("prune after snapshot %d failed: %v", height, err)
	}
	return meta, nil
}

// ListSnapshots returns the committed, verified snapshots, ascending.
func (s *Store) ListSnapshots() []SnapshotMeta {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]SnapshotMeta, len(s.manifest.Snapshots))
	copy(out, s.manifest.Snapshots)
	return out
}

// Prune deletes superseded snapshots and the deltas they cover, keeping the
// newest KeepSnapshots snapshots. Deltas and snapshots pinned by an active
// (unexpired) reader lease are never deleted.
func (s *Store) Prune(ctx context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	snaps := s.manifest.Snapshots
	if len(snaps) == 0 {
		return nil
	}
	keepFrom := len(snaps) - s.opts.KeepSnapshots
	if keepFrom < 0 {
		keepFrom = 0
	}
	retained := snaps[keepFrom:]
	candidates := snaps[:keepFrom]

	// Deltas with height <= floor are fully covered by a retained snapshot.
	floor := retained[0].Height
	removed := map[uint64]bool{}
	kept := append([]SnapshotMeta{}, retained...)
	for _, m := range candidates {
		if s.readers.hasActiveBase(m.Height, s.opts.Now()) {
			kept = append(kept, m) // pinned by an active reader
			continue
		}
		removed[m.Height] = true
	}
	sort.Slice(kept, func(i, j int) bool { return kept[i].Height < kept[j].Height })
	if b, ok := s.readers.minActiveBase(s.opts.Now()); ok && b < floor {
		floor = b
	}

	batch := s.db.NewBatch()
	defer batch.Close()
	// Only delete deltas when snapshots were actually evicted: deltas at or
	// below floor are then fully covered by a retained (or reader-pinned)
	// snapshot. With no evictions every historical height stays servable.
	if len(removed) > 0 && floor > 0 {
		// Delete deltas [1, floor]: covered by the snapshot at floor (or by
		// the active reader's own base snapshot).
		if err := batch.DeleteRange(deltaKey(0), deltaKey(floor+1), nil); err != nil {
			return err
		}
	}
	for h := range removed {
		if err := deleteSnapshotDataBatch(batch, h); err != nil {
			return err
		}
	}
	manData, err := json.Marshal(Manifest{Snapshots: kept})
	if err != nil {
		return err
	}
	if err := batch.Set([]byte(keyManifest), manData, nil); err != nil {
		return err
	}
	if err := batch.Commit(pebble.Sync); err != nil {
		return err
	}
	if len(removed) > 0 {
		s.opts.Logger("prune: removed %d snapshots, deleted deltas <= %d, retained %d snapshots",
			len(removed), floor, len(kept))
	}
	s.manifest.Snapshots = kept
	return nil
}

// --- key encodings ---

func deltaKey(h uint64) []byte {
	k := make([]byte, len("delta/")+8)
	copy(k, "delta/")
	binary.BigEndian.PutUint64(k[len("delta/"):], h)
	return k
}

func snapDataPrefix(h uint64) string { return fmt.Sprintf("snap/data/%020d/", h) }
func snapSealKey(h uint64) string    { return fmt.Sprintf("snap/seal/%020d", h) }

// prefixUpper returns the smallest key greater than every key with prefix.
func prefixUpper(prefix string) string {
	b := []byte(prefix)
	for i := len(b) - 1; i >= 0; i-- {
		if b[i] != 0xff {
			b[i]++
			return string(b[:i+1])
		}
	}
	return prefix + "\x00"
}

// --- state helpers ---

func applyChanges(state map[string]int64, changes map[string]int64) {
	for acct, delta := range changes {
		bal := state[acct] + delta
		if bal == 0 {
			delete(state, acct)
		} else {
			state[acct] = bal
		}
	}
}

func totalBalance(state map[string]int64) int64 {
	var sum int64
	for _, b := range state {
		sum += b
	}
	return sum
}

func sortedKeys(state map[string]int64) []string {
	keys := make([]string, 0, len(state))
	for k := range state {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// hashState is the SHA-256 of the canonical "acct=balance\n" encoding.
func hashState(state map[string]int64) string {
	h := sha256.New()
	for _, k := range sortedKeys(state) {
		fmt.Fprintf(h, "%s=%d\n", k, state[k])
	}
	return hex.EncodeToString(h.Sum(nil))
}

func (s *Store) loadSnapshotData(ctx context.Context, height uint64, deadline time.Time) (map[string]int64, error) {
	prefix := snapDataPrefix(height)
	it, err := s.db.NewIter(&pebble.IterOptions{
		LowerBound: []byte(prefix),
		UpperBound: []byte(prefixUpper(prefix)),
	})
	if err != nil {
		return nil, err
	}
	defer it.Close()
	state := map[string]int64{}
	n := 0
	for it.First(); it.Valid(); it.Next() {
		if !deadline.IsZero() && n%64 == 0 && s.opts.Now().After(deadline) {
			return nil, ErrReaderTimeout
		}
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		acct := string(it.Key()[len(prefix):])
		bal, err := strconv.ParseInt(string(it.Value()), 10, 64)
		if err != nil {
			return nil, fmt.Errorf("corrupt snapshot balance for %q: %w", acct, err)
		}
		state[acct] = bal
		n++
	}
	if err := it.Error(); err != nil {
		return nil, err
	}
	return state, nil
}

func (s *Store) deleteSnapshotData(height uint64) {
	batch := s.db.NewBatch()
	defer batch.Close()
	if err := deleteSnapshotDataBatch(batch, height); err != nil {
		s.opts.Logger("recovery: delete snapshot %d failed: %v", height, err)
		return
	}
	if err := batch.Commit(pebble.Sync); err != nil {
		s.opts.Logger("recovery: commit delete snapshot %d failed: %v", height, err)
	}
}

func deleteSnapshotDataBatch(batch *pebble.Batch, height uint64) error {
	prefix := snapDataPrefix(height)
	if err := batch.DeleteRange([]byte(prefix), []byte(prefixUpper(prefix)), nil); err != nil {
		return err
	}
	return batch.Delete([]byte(snapSealKey(height)), nil)
}

// persistManifestLocked writes the in-memory manifest. Caller holds s.mu.
func (s *Store) persistManifestLocked() error {
	data, err := json.Marshal(s.manifest)
	if err != nil {
		return err
	}
	return s.db.Set([]byte(keyManifest), data, pebble.Sync)
}

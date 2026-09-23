// Package engine ties storage, state execution and snapshot management
// together. It enforces:
//
//   - serialized block append (new blocks still land while a snapshot is
//     being built, because construction reads a Pebble snapshot);
//   - history queries that overlay deltas onto the latest snapshot;
//   - reader leases that pin both the snapshot file and the delta range,
//     with explicit failure on lease expiry;
//   - retention pruning (3 snapshots) and delta pruning that can never
//     remove data an active reader depends on.
package engine

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"

	"github.com/example/snapshotprune/internal/crypto"
	"github.com/example/snapshotprune/internal/snapshot"
	"github.com/example/snapshotprune/internal/state"
	"github.com/example/snapshotprune/internal/store"
	"github.com/example/snapshotprune/internal/types"
)

// ErrSimulatedCrash is a test-only sentinel. When the build hook returns
// it, BuildSnapshot skips cleanup, leaving the .building directory on disk
// exactly as a real process crash would (no manifest, reservation dangling).
var ErrSimulatedCrash = errors.New("simulated crash during snapshot build")

// Sentinel errors, mapped to HTTP statuses by the server package.
var (
	ErrNoGenesis       = errors.New("chain has no genesis")
	ErrBadHeight       = errors.New("requested height is ahead of tip")
	ErrBadParent       = errors.New("block parent hash does not match tip")
	ErrBadRoot         = errors.New("block state root does not match execution")
	ErrBadTxRoot       = errors.New("block tx root does not match transactions")
	ErrMissingDeltas   = errors.New("deltas required to reconstruct this height have been pruned")
	ErrLeaseExpired    = errors.New("reader lease expired")
	ErrLeaseInvalid    = errors.New("reader lease not found")
	ErrLeaseBadHeight  = errors.New("lease height is ahead of tip")
	ErrSnapshotBusy    = errors.New("snapshot build already in progress")
	ErrSnapshotExists  = errors.New("snapshot already exists at height")
	ErrSnapshotMissing = errors.New("snapshot not found")
)

// Clock allows tests to control time.
type Clock interface {
	Now() time.Time
}

type wallClock struct{}

func (wallClock) Now() time.Time { return time.Now() }

// Lease is an explicit reader pin.
type Lease struct {
	ID        string    `json:"id"`
	Height    uint64    `json:"height"`
	Base      uint64    `json:"base"` // snapshot or 0 the query starts from
	CreatedAt time.Time `json:"created_at"`
	ExpiresAt time.Time `json:"expires_at"`
}

// Config holds engine parameters.
type Config struct {
	DataDir        string
	ChainID        string
	Genesis        *types.Genesis // required only on first boot
	LeaseTTL       time.Duration
	SnapshotPeriod time.Duration // 0 disables periodic construction
}

// Engine is the main service object.
type Engine struct {
	cfg     Config
	store   *store.Store
	snaps   *snapshot.Manager
	nodeKey *crypto.NodeKey
	clock   Clock

	// Serializes writers (append/build-finalize/prune). Readers hold RLock
	// for the whole reconstruct, which is what keeps required data on disk.
	mu sync.RWMutex

	leaseMu     sync.Mutex
	leases      map[string]*Lease
	leasePinsMu sync.Mutex
	leasePins   []pinEntry

	buildHook  func() error // test-only fault injection
	stopBuilds chan struct{}
}

// Open bootstraps or opens the service.
func Open(cfg Config) (*Engine, error) {
	if cfg.LeaseTTL <= 0 {
		cfg.LeaseTTL = 5 * time.Second
	}
	st, err := store.Open(cfg.DataDir)
	if err != nil {
		return nil, err
	}
	key, err := crypto.LoadOrCreateNodeKey(cfg.DataDir)
	if err != nil {
		st.Close()
		return nil, err
	}
	tip, err := st.Tip()
	if errors.Is(err, store.ErrNotFound) {
		// First boot: require genesis.
		if cfg.Genesis == nil {
			st.Close()
			return nil, ErrNoGenesis
		}
		if err := initGenesis(st, cfg.Genesis); err != nil {
			st.Close()
			return nil, err
		}
		tip = 0
	} else if err != nil {
		st.Close()
		return nil, err
	}
	if cfg.Genesis != nil {
		// Store chain id for future reference but do not re-init an existing DB.
		_ = st.SetMeta("chain_id", []byte(cfg.ChainID))
	} else {
		if v, err := st.GetMeta("chain_id"); err == nil {
			cfg.ChainID = string(v)
		}
	}
	mgr, err := snapshot.NewManager(cfg.DataDir, cfg.ChainID, key)
	if err != nil {
		st.Close()
		return nil, err
	}
	e := &Engine{
		cfg:        cfg,
		store:      st,
		snaps:      mgr,
		nodeKey:    key,
		clock:      wallClock{},
		leases:     map[string]*Lease{},
		stopBuilds: make(chan struct{}),
	}
	if _, err := mgr.Recover(); err != nil {
		st.Close()
		return nil, err
	}
	_ = tip
	return e, nil
}

func initGenesis(st *store.Store, g *types.Genesis) error {
	tbl := state.Table{}
	for _, a := range g.Allocations {
		addr, err := types.ParseAddress(a.Address)
		if err != nil {
			return fmt.Errorf("genesis allocation: %w", err)
		}
		if _, dup := tbl[addr]; dup {
			return fmt.Errorf("duplicate genesis allocation for %s", a.Address)
		}
		tbl[addr] = &types.Account{Nonce: 0, Balance: a.Balance}
	}
	root := state.Root(tbl)
	hdr := &types.Header{
		Height:     0,
		ParentHash: types.ZeroHash,
		StateRoot:  root,
		TxRoot:     types.TxRoot(nil),
		Timestamp:  0,
	}
	genesis := &types.Block{Header: *hdr}
	if err := st.PutGenesis(genesis); err != nil {
		return err
	}
	gbytes, _ := jsonGenesis(g)
	if err := st.SetMeta("genesis", gbytes); err != nil {
		return err
	}
	if err := st.SetMeta("chain_id", []byte(g.ChainID)); err != nil {
		return err
	}
	return nil
}

// Close releases resources and stops the periodic builder.
func (e *Engine) Close() error {
	close(e.stopBuilds)
	return e.store.Close()
}

// SetClock is test-only.
func (e *Engine) SetClock(c Clock) { e.clock = c }

// SetBuildHook is test-only fault injection during snapshot construction
// (invoked after the state file is durable, before the manifest is written).
func (e *Engine) SetBuildHook(f func() error) { e.buildHook = f }

// Store / Manager accessors (server).
func (e *Engine) Store() *store.Store      { return e.store }
func (e *Engine) Snaps() *snapshot.Manager { return e.snaps }
func (e *Engine) NodePubHex() string       { return e.nodeKey.PubHex() }
func (e *Engine) ChainID() string          { return e.cfg.ChainID }

// Tip returns the current tip height.
func (e *Engine) Tip() (uint64, error) { return e.store.Tip() }

// GenesisBlock returns the height-0 block.
func (e *Engine) GenesisBlock() (*types.Block, error) { return e.store.Block(0) }

// Block returns a block by height.
func (e *Engine) Block(h uint64) (*types.Block, error) { return e.store.Block(h) }

// ---- block append ----

// Proposal is the un-executed block submitted by a client.
type Proposal struct {
	Height    uint64     `json:"height"`
	Timestamp int64      `json:"timestamp_unix_nano"`
	Txs       []types.Tx `json:"txs"`
}

// AppendProposal validates signatures and chain linkage, executes the block
// against the current state (reconstructed from snapshot+deltas), stores
// block and delta atomically, and returns the committed block.
//
// The execution lock is held only for the short validate+commit section;
// reads for reconstruction use a Pebble snapshot via the query path.
func (e *Engine) AppendProposal(p *Proposal) (*types.Block, error) {
	tip, err := e.store.Tip()
	if err != nil {
		return nil, err
	}
	if p.Height != tip+1 {
		return nil, fmt.Errorf("%w: expected %d, got %d", ErrBadHeight, tip+1, p.Height)
	}
	for i := range p.Txs {
		if err := crypto.CheckTx(&p.Txs[i]); err != nil {
			return nil, fmt.Errorf("tx %d invalid: %w", i, err)
		}
	}

	e.mu.Lock()
	defer e.mu.Unlock()

	tip, err = e.store.Tip()
	if err != nil {
		return nil, err
	}
	if p.Height != tip+1 {
		return nil, fmt.Errorf("%w: concurrent append, expected %d", ErrBadHeight, tip+1)
	}
	tipBlock, err := e.store.Block(tip)
	if err != nil {
		return nil, err
	}

	// Reconstruct pre-state, then execute.
	tbl, _, err := e.reconstructLocked(tip, nil)
	if err != nil {
		return nil, err
	}
	delta, err := state.Apply(tbl, p.Height, p.Txs)
	if err != nil {
		return nil, err
	}
	root := state.Root(tbl)
	txRoot := types.TxRoot(p.Txs)
	if p.Timestamp == 0 {
		p.Timestamp = e.clock.Now().UnixNano()
	}
	blk := &types.Block{
		Header: types.Header{
			Height:     p.Height,
			ParentHash: tipBlock.Hash(),
			StateRoot:  root,
			TxRoot:     txRoot,
			Timestamp:  p.Timestamp,
		},
		Txs: p.Txs,
	}
	if err := e.store.PutBlockAndDelta(blk, delta); err != nil {
		return nil, err
	}
	return blk, nil
}

// ---- reader leases ----

// CreateLease pins a height for reading. The pin covers the chosen snapshot
// file (via the snapshot manager) and the delta range (via the lease map
// consulted during pruning). A lease is refused explicitly when the height
// cannot currently be reconstructed because its deltas were already pruned.
func (e *Engine) CreateLease(height uint64) (*Lease, error) {
	e.mu.RLock()
	defer e.mu.RUnlock()
	tip, err := e.store.Tip()
	if err != nil {
		return nil, err
	}
	if height > tip {
		return nil, fmt.Errorf("%w: %d > tip %d", ErrLeaseBadHeight, height, tip)
	}
	base, err := e.baseForLocked(height)
	if err != nil {
		return nil, err
	}
	// Verify the whole snapshot+delta chain is present. This and the pin
	// registration below happen while holding e.mu.RLock, which excludes
	// Prune (which takes the write lock), so the verified data cannot be
	// deleted before the lease is registered.
	if err := e.checkContinuityLocked(base, height); err != nil {
		return nil, err
	}

	var raw [12]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return nil, err
	}
	id := hex.EncodeToString(raw[:])
	now := e.clock.Now()
	l := &Lease{
		ID:        id,
		Height:    height,
		Base:      base,
		CreatedAt: now,
		ExpiresAt: now.Add(e.cfg.LeaseTTL),
	}

	e.leaseMu.Lock()
	defer e.leaseMu.Unlock()
	if base > 0 {
		// Pin the snapshot file for the whole lease lifetime. The pin is
		// registered together with the lease so they cannot diverge.
		release, err := e.snaps.Acquire(base)
		if err != nil {
			return nil, err
		}
		e.leasePins = append(e.leasePins, pinEntry{leaseID: id, base: base, release: release})
	}
	e.leases[id] = l
	return l, nil
}

// detachPinsFor removes and returns snapshot release funcs owned by leaseID.
// leasePinsMu is acquired independently; leaseMu may be held by the caller.
func (e *Engine) detachPinsFor(leaseID string) []func() {
	e.leasePinsMu.Lock()
	defer e.leasePinsMu.Unlock()
	var out []func()
	kept := e.leasePins[:0]
	for _, p := range e.leasePins {
		if p.leaseID == leaseID {
			out = append(out, p.release)
		} else {
			kept = append(kept, p)
		}
	}
	e.leasePins = kept
	return out
}

// QueryAtWithLease reconstructs state at a height covered by the lease.
// Fails explicitly (410) when the lease has expired.
func (e *Engine) QueryAtWithLease(leaseID string, account *types.Address) (*state.AccountView, string, error) {
	e.leaseMu.Lock()
	l, ok := e.leases[leaseID]
	if !ok {
		e.leaseMu.Unlock()
		return nil, "", ErrLeaseInvalid
	}
	if !e.clock.Now().Before(l.ExpiresAt) {
		delete(e.leases, leaseID)
		pins := e.detachPinsFor(leaseID)
		e.leaseMu.Unlock()
		for _, p := range pins {
			p()
		}
		return nil, "", ErrLeaseExpired
	}
	e.leaseMu.Unlock()

	e.mu.RLock()
	defer e.mu.RUnlock()
	tbl, src, err := e.reconstructLocked(l.Height, l)
	if err != nil {
		return nil, "", err
	}
	v := &state.AccountView{Summary: state.Describe(tbl, l.Height)}
	if account != nil {
		a := *account
		v.Address = &a
		if acc, ok := tbl[*account]; ok {
			cp := *acc
			v.Account = &cp
		}
	}
	return v, src, nil
}

// ReleaseLease ends a lease early and drops its snapshot pins.
func (e *Engine) ReleaseLease(id string) error {
	e.leaseMu.Lock()
	l, ok := e.leases[id]
	if !ok {
		e.leaseMu.Unlock()
		return ErrLeaseInvalid
	}
	delete(e.leases, id)
	pins := e.detachPinsFor(id)
	e.leaseMu.Unlock()
	_ = l
	for _, p := range pins {
		p()
	}
	return nil
}

// Lease returns a copy for API listing.
func (e *Engine) Lease(id string) (*Lease, error) {
	e.leaseMu.Lock()
	defer e.leaseMu.Unlock()
	l, ok := e.leases[id]
	if !ok {
		return nil, ErrLeaseInvalid
	}
	cp := *l
	return &cp, nil
}

// purgeExpiredLeases is the lock-free wrapper used by callers.
func (e *Engine) purgeExpiredLeases() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.purgeExpiredLeasesLocked()
}

// purgeExpiredLeasesLocked drops expired leases and their snapshot pins.
// e.mu (write) must be held, matching prune serialization.
func (e *Engine) purgeExpiredLeasesLocked() int {
	now := e.clock.Now()
	e.leaseMu.Lock()
	var releases [][]func()
	n := 0
	for id, l := range e.leases {
		if !now.Before(l.ExpiresAt) {
			delete(e.leases, id)
			releases = append(releases, e.detachPinsFor(id))
			n++
		}
	}
	e.leaseMu.Unlock()
	for _, ps := range releases {
		for _, p := range ps {
			p()
		}
	}
	return n
}

// Reconstruct runs the live snapshot+delta query path without a lease.
// It implements replay.QueryReconstructor for cross-check tooling.
func (e *Engine) Reconstruct(height uint64) (state.Table, error) {
	e.mu.RLock()
	defer e.mu.RUnlock()
	tip, err := e.store.Tip()
	if err != nil {
		return nil, err
	}
	if height > tip {
		return nil, fmt.Errorf("%w: %d > tip %d", ErrBadHeight, height, tip)
	}
	tbl, _, err := e.reconstructLocked(height, nil)
	return tbl, err
}

// ---- internal state reconstruction ----

// baseForLocked chooses the highest snapshot at or below height. Returns 0
// when genesis will be used (which is always available).
func (e *Engine) baseForLocked(height uint64) (uint64, error) {
	if h, ok := e.snaps.LatestAtOrBelow(height); ok {
		return h, nil
	}
	return 0, nil
}

// checkContinuityLocked verifies every delta in (base, height] is present.
// e.mu must be held (RLock suffices).
func (e *Engine) checkContinuityLocked(base, height uint64) error {
	for h := base + 1; h <= height; h++ {
		ok, err := e.store.HasDelta(h)
		if err != nil {
			return err
		}
		if !ok {
			return fmt.Errorf("%w: height %d (base snapshot %d)", ErrMissingDeltas, h, base)
		}
	}
	return nil
}

// reconstructLocked loads base state and overlays deltas in (base, height].
// The passed lease (may be nil) is only used to verify continuity; missing
// deltas produce ErrMissingDeltas so the API can fail explicitly.
func (e *Engine) reconstructLocked(height uint64, l *Lease) (state.Table, string, error) {
	base, err := e.baseForLocked(height)
	if err != nil {
		return nil, "", err
	}
	if l != nil && l.Base != base {
		// Snapshot set changed under the lease (retention cannot have deleted
		// its pinned snapshot, but a newer snapshot could appear). Rebind:
		// the lease still pins its original base; use that one.
		base = l.Base
	}

	var tbl state.Table
	src := "genesis"
	if base > 0 {
		release, err := e.snaps.Acquire(base)
		if err != nil {
			return nil, "", err
		}
		tbl, err = e.snaps.Load(base)
		release()
		if err != nil {
			return nil, "", err
		}
		src = fmt.Sprintf("snapshot@%d", base)
	} else {
		var err error
		tbl, err = e.genesisTable()
		if err != nil {
			return nil, "", err
		}
	}

	// Overlay deltas. The Pebble keyspace may have been pruned up to a floor;
	// any gap makes the requested height unreadable by design.
	if err := e.checkContinuityLocked(base, height); err != nil {
		return nil, "", err
	}
	for h := base + 1; h <= height; h++ {
		d, err := e.store.Delta(h)
		if err != nil {
			return nil, "", err
		}
		state.ApplyDelta(tbl, d)
	}
	return tbl, src, nil
}

func (e *Engine) genesisTable() (state.Table, error) {
	gb, err := e.store.GetMeta("genesis")
	if err != nil {
		return nil, err
	}
	var g types.Genesis
	if err := jsonGenesisDecode(gb, &g); err != nil {
		return nil, err
	}
	tbl := state.Table{}
	for _, a := range g.Allocations {
		addr, err := types.ParseAddress(a.Address)
		if err != nil {
			return nil, err
		}
		tbl[addr] = &types.Account{Balance: a.Balance}
	}
	return tbl, nil
}

// ---- snapshot construction ----

// BuildSnapshot constructs a full snapshot at height. Construction runs
// from a Pebble point-in-time snapshot so new blocks appended concurrently
// are not included: the snapshot is bound to the requested height.
//
// This is synchronous from the caller's perspective; the lock rules make
// sure appends can proceed in parallel (build only takes RLock).
func (e *Engine) BuildSnapshot(height uint64) (*snapshot.Info, error) {
	tip, err := e.store.Tip()
	if err != nil {
		return nil, err
	}
	if height > tip {
		return nil, fmt.Errorf("%w: %d > tip %d", ErrBadHeight, height, tip)
	}
	if e.snaps.Has(height) {
		return nil, fmt.Errorf("%w: height %d", ErrSnapshotExists, height)
	}

	buildID, err := e.snaps.BeginBuild(height)
	if err != nil {
		if errors.Is(err, snapshot.ErrAlreadyBuilding) {
			return nil, ErrSnapshotBusy
		}
		if errors.Is(err, snapshot.ErrExists) {
			return nil, fmt.Errorf("%w: height %d", ErrSnapshotExists, height)
		}
		return nil, err
	}

	if err := e.buildFromPebbleSnapshot(height, buildID); err != nil {
		// On a simulated crash we deliberately leave the .building dir and
		// reservation in place to model a hard process exit mid-commit.
		if !errors.Is(err, ErrSimulatedCrash) {
			_ = e.snaps.AbortBuild(height, buildID)
		}
		return nil, err
	}
	infos := e.snaps.List()
	for i := range infos {
		if infos[i].Height == height {
			return &infos[i], nil
		}
	}
	return nil, errors.New("snapshot committed but not listed")
}

func (e *Engine) buildFromPebbleSnapshot(height uint64, buildID string) error {
	// Read everything from a consistent Pebble snapshot.
	rd, closeRd, err := e.store.NewSnapshotReader()
	if err != nil {
		return err
	}
	defer closeRd()

	sTip, err := rd.Tip()
	if err != nil {
		return err
	}
	if height > sTip {
		return fmt.Errorf("%w: height %d > snapshot tip %d", ErrBadHeight, height, sTip)
	}

	// Pick base within this read view: must already be durable and verified.
	var base uint64
	if h, ok := e.snaps.LatestAtOrBelow(height); ok {
		base = h
	}
	tbl := state.Table{}
	if base > 0 {
		pin, err := e.snaps.Acquire(base)
		if err != nil {
			return err
		}
		tbl, err = e.snaps.Load(base)
		pin()
		if err != nil {
			return err
		}
	} else {
		gbytes, err := e.store.GetMeta("genesis")
		if err != nil {
			return err
		}
		var g types.Genesis
		if err := jsonGenesisDecode(gbytes, &g); err != nil {
			return err
		}
		for _, a := range g.Allocations {
			addr, err := types.ParseAddress(a.Address)
			if err != nil {
				return err
			}
			tbl[addr] = &types.Account{Balance: a.Balance}
		}
	}

	// Overlay deltas via the point-in-time reader; verify continuity.
	for h := base + 1; h <= height; h++ {
		d, err := rd.Delta(h)
		if errors.Is(err, store.ErrNotFound) {
			return fmt.Errorf("%w: gap at height %d (base %d) during snapshot build", ErrMissingDeltas, h, base)
		}
		if err != nil {
			return err
		}
		state.ApplyDelta(tbl, d)
	}

	// Cross-check the computed root against the stored block header at the
	// bound height — detects any delta/log corruption before publishing.
	blk, err := rd.Block(height)
	if err != nil {
		return err
	}
	if got := state.Root(tbl); got != blk.Header.StateRoot {
		return fmt.Errorf("%w: computed %s, block stores %s at height %d",
			ErrBadRoot, got.Hex(), blk.Header.StateRoot.Hex(), height)
	}

	return e.snaps.Commit(height, buildID, tbl, e.buildHook)
}

// ---- pruning ----

// PruneReport is the full pruning result.
type PruneReport struct {
	Snapshots *snapshot.PruneResult `json:"snapshots"`
	// DeltaDeleteThrough is the height through which delta records were
	// deleted; 0 means nothing was deleted.
	DeltaDeleteThrough uint64 `json:"delta_delete_through"`
	// DeltaFloor is the oldest retained snapshot height that defines the
	// reconstructable boundary.
	DeltaFloor uint64 `json:"delta_floor"`
	// DeltaBlockedByLeases lists lease ids whose bases prevented more pruning.
	DeltaBlockedByLeases []LeaseBlock `json:"delta_blocked_by_leases,omitempty"`
	ExpiredLeasesPurged  int          `json:"expired_leases_purged"`
}

// LeaseBlock explains why delta pruning was limited.
type LeaseBlock struct {
	LeaseID string `json:"lease_id"`
	Height  uint64 `json:"height"`
	Base    uint64 `json:"base"`
}

// Prune applies retention and delta pruning. Active leases always protect
// the snapshot+delta range they were created against.
func (e *Engine) Prune() (*PruneReport, error) {
	e.mu.Lock()
	defer e.mu.Unlock()

	rep := &PruneReport{}
	rep.ExpiredLeasesPurged = e.purgeExpiredLeasesLocked()

	res, err := e.snaps.PruneOld()
	if err != nil {
		return nil, err
	}
	rep.Snapshots = res

	floor, hasFloor := e.snaps.OldestKept()
	if !hasFloor {
		// No snapshots at all: keep all deltas.
		return rep, nil
	}
	rep.DeltaFloor = floor

	// Active leases cap how far deltas may be deleted: a lease at height H
	// with base B needs every delta in (B, H]. So deltas may be deleted
	// only through min(floor-1, minBase-1) across active leases... but a
	// lease whose base >= floor constrains us further to base-1, and a
	// genesis-based lease (base 0) forbids all delta deletion.
	target := floor - 1 // safe default: delete below oldest kept snapshot
	if floor == 0 {
		target = 0
	}
	e.leaseMu.Lock()
	now := e.clock.Now()
	var blocks []LeaseBlock
	for id, l := range e.leases {
		if !now.Before(l.ExpiresAt) {
			continue
		}
		if l.Base == 0 {
			target = 0
			blocks = append(blocks, LeaseBlock{id, l.Height, l.Base})
			continue
		}
		if l.Base-1 < target {
			target = l.Base - 1
			blocks = append(blocks, LeaseBlock{id, l.Height, l.Base})
		}
	}
	e.leaseMu.Unlock()
	sort.Slice(blocks, func(i, j int) bool { return blocks[i].LeaseID < blocks[j].LeaseID })
	rep.DeltaBlockedByLeases = blocks

	// Never delete genesis delta (height 0); target is naturally 0-based.
	if target >= 1 {
		n, err := e.store.PruneDeltas(target)
		if err != nil {
			return nil, err
		}
		if n > 0 {
			rep.DeltaDeleteThrough = target
		}
	}
	return rep, nil
}

// ActiveLeases returns copies of active leases.
func (e *Engine) ActiveLeases() []Lease {
	e.leaseMu.Lock()
	defer e.leaseMu.Unlock()
	now := e.clock.Now()
	out := make([]Lease, 0, len(e.leases))
	for _, l := range e.leases {
		if now.Before(l.ExpiresAt) {
			out = append(out, *l)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// StartPeriodic runs the background snapshot/prune loop until Close.
func (e *Engine) StartPeriodic() {
	if e.cfg.SnapshotPeriod <= 0 {
		return
	}
	go func() {
		t := time.NewTicker(e.cfg.SnapshotPeriod)
		defer t.Stop()
		for {
			select {
			case <-e.stopBuilds:
				return
			case <-t.C:
				tip, err := e.store.Tip()
				if err != nil {
					continue
				}
				if e.snaps.Has(tip) {
					// Still prune so retention/delta GC runs.
					_, _ = e.Prune()
					continue
				}
				if _, err := e.BuildSnapshot(tip); err != nil {
					// busy/exists are benign races
					if !errors.Is(err, ErrSnapshotBusy) && !errors.Is(err, ErrSnapshotExists) {
						// surfaced via logs by caller; ignore in loop
						_ = err
					}
				}
				_, _ = e.Prune()
			}
		}
	}()
}

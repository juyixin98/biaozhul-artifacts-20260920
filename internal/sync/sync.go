// Package sync implements anti-entropy Merkle synchronization between two
// replicas over the HTTP API in package server.
//
// One sync round (both sides pin immutable snapshots, compare fixed
// Merkle trees top-down, exchange only entries of differing buckets, then
// commit with an epoch guard) is implemented by diffRound. Run retries the
// whole round whenever it detects that either tree root moved during the
// scan:
//
//   - HTTP 409 "epoch_moved"   -> the other side committed past its pinned
//     snapshot; its local apply (and ours) is discarded and we restart.
//   - HTTP 404 snapshot_*      -> the other side evicted the snapshot
//     (TTL / capacity); restart.
//   - local epoch-moved result -> our own store changed; remote apply is
//     guaranteed to have been skipped on the remote's epoch guard too, so
//     nothing is partially applied; restart.
//   - post-apply root mismatch -> belt-and-braces check, restart.
//
// Merge is last-writer-wins per key (higher HLC version, origin string
// breaks exact ties) with tombstones, so the union merge is commutative and
// idempotent: retrying rounds cannot lose or resurrect data, and once both
// roots are equal the replicas hold identical versioned state.
package sync

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"sort"
	"time"

	"github.com/example/merklekv/internal/hlc"
	"github.com/example/merklekv/internal/merkle"
	"github.com/example/merklekv/internal/store"
)

// Defaults for a sync run.
const (
	DefaultMaxRounds = 10
	batchPaths       = 256
	batchBuckets     = 16
)

// errSnapshotMoved signals that the peer's state moved during a scan-phase
// request; the round must be retried with fresh snapshots.
var errSnapshotMoved = errors.New("sync: peer snapshot moved during the scan")

// moveErr reports whether err is a detectable root/snapshot move: either the
// explicit sentinel or a peer 409/404. It returns the peer error so the
// caller can cite its code in conflict diagnostics.
func moveErr(err error) (*apiError, bool) {
	if err == nil {
		return nil, false
	}
	if errors.Is(err, errSnapshotMoved) {
		return &apiError{code: "snapshot_moved"}, true
	}
	var ae *apiError
	if errors.As(err, &ae) && (ae.status == http.StatusConflict || ae.status == http.StatusNotFound) {
		return ae, true
	}
	return nil, false
}

// Config configures a Client.
type Config struct {
	Local     *store.Store
	Params    merkle.Params
	Peer      string // base URL of the other replica, e.g. http://127.0.0.1:8081
	MaxRounds int    // retry cap for root-move detection; default 10
	BaseDelay time.Duration
	MaxDelay  time.Duration
	HTTP      *http.Client
	// OnConflict is invoked once after a round detects a root/epoch move.
	// Tests use it to assert retries happened.
	OnConflict func(round int, reason string)
	// BetweenPhase is invoked after the differing buckets are fetched but
	// before either side applies, while both snapshots stay pinned. The
	// argument is the current round (1-based). Tests use it to mutate a
	// store once to simulate an update during the scan.
	BetweenPhase func(round int)
	// BeforePeerRequest is invoked before every HTTP request sent to the
	// peer (attempt-scoped, 0-based). Tests use it to evict the peer's
	// snapshots mid-round to exercise the 404 -> retry path.
	BeforePeerRequest func(attempt int, path string)
}

// Stats reports what a sync actually exchanged, so callers can verify it
// was sublinear in total data size.
type Stats struct {
	Converged            bool          `json:"converged"`
	RoundsAttempted      int           `json:"rounds_attempted"`
	RootMovesDetected    int           `json:"root_moves_detected"`
	HashNodesExchanged   int           `json:"hash_nodes_exchanged"`
	LeafHashNodes        int           `json:"leaf_hash_nodes_exchanged"`
	InternalHashNodes    int           `json:"internal_hash_nodes_exchanged"`
	BucketsOpened        int           `json:"buckets_opened"`
	EntriesExchanged     int           `json:"entries_exchanged"` // entries sent + fetched
	EntriesFetched       int           `json:"entries_fetched"`
	EntriesSent          int           `json:"entries_sent"`
	UserBytesExchanged   int64         `json:"user_bytes_exchanged"` // keys+values that moved
	WireBytesExchanged   int64         `json:"wire_bytes_exchanged"` // HTTP bodies
	BaselineFullTransfer int64         `json:"baseline_full_transfer_bytes"`
	Duration             time.Duration `json:"duration"`
}

// Result is the full Run outcome, which also marshals the stats fields.
type Result struct {
	Stats
	LocalRoot  string `json:"local_root"`
	RemoteRoot string `json:"remote_root"`
}

// Client synchronizes one local store against one peer.
type Client struct {
	cfg     Config
	rng     *rand.Rand
	round   int // current round (1-based)
	attempt int // HTTP attempt counter, reset at the start of each round
}

// NewClient validates the configuration.
func NewClient(cfg Config) (*Client, error) {
	if cfg.Local == nil {
		return nil, errors.New("sync: nil local store")
	}
	if cfg.Peer == "" {
		return nil, errors.New("sync: empty peer URL")
	}
	if cfg.MaxRounds <= 0 {
		cfg.MaxRounds = DefaultMaxRounds
	}
	if cfg.BaseDelay <= 0 {
		cfg.BaseDelay = 5 * time.Millisecond
	}
	if cfg.MaxDelay <= 0 {
		cfg.MaxDelay = 250 * time.Millisecond
	}
	if cfg.HTTP == nil {
		cfg.HTTP = http.DefaultClient
	}
	return &Client{cfg: cfg, rng: rand.New(rand.NewSource(time.Now().UnixNano()))}, nil
}

// Run retries rounds until convergence or MaxRounds root moves are hit.
func (c *Client) Run(ctx context.Context) (*Result, error) {
	start := time.Now()
	var totals Stats
	var localRoot, remoteRoot string

	for round := 1; round <= c.cfg.MaxRounds; round++ {
		c.round = round
		totals.RoundsAttempted = round
		r, moved, err := c.diffRound(ctx)
		totals.HashNodesExchanged += r.HashNodesExchanged
		totals.LeafHashNodes += r.LeafHashNodes
		totals.InternalHashNodes += r.InternalHashNodes
		totals.BucketsOpened += r.BucketsOpened
		totals.EntriesExchanged += r.EntriesExchanged
		totals.EntriesFetched += r.EntriesFetched
		totals.EntriesSent += r.EntriesSent
		totals.UserBytesExchanged += r.UserBytesExchanged
		totals.WireBytesExchanged += r.WireBytesExchanged
		totals.BaselineFullTransfer = r.BaselineFullTransfer
		localRoot, remoteRoot = r.LocalRoot, r.RemoteRoot
		if err != nil {
			return nil, err
		}
		if !moved {
			totals.Converged = true
			totals.Duration = time.Since(start)
			return &Result{Stats: totals, LocalRoot: localRoot, RemoteRoot: remoteRoot}, nil
		}
		totals.RootMovesDetected++
		if c.cfg.OnConflict != nil {
			c.cfg.OnConflict(round, r.ConflictReason)
		}
		if round == c.cfg.MaxRounds {
			break
		}
		if err := c.sleepBackoff(ctx, round); err != nil {
			return nil, err
		}
	}
	totals.Duration = time.Since(start)
	return nil, fmt.Errorf("sync: giving up after %d rounds; roots still differ (local=%s remote=%s)",
		c.cfg.MaxRounds, short(localRoot), short(remoteRoot))
}

func (c *Client) sleepBackoff(ctx context.Context, round int) error {
	d := c.cfg.BaseDelay << min(round-1, 6)
	if d > c.cfg.MaxDelay {
		d = c.cfg.MaxDelay
	}
	d += time.Duration(c.rng.Int63n(int64(c.cfg.BaseDelay)))
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func short(s string) string {
	if len(s) > 12 {
		return s[:12] + "…"
	}
	return s
}

// ---- wire DTOs ------------------------------------------------------------

type snapshotInfo struct {
	ID        string `json:"snapshot_id"`
	Epoch     int64  `json:"epoch"`
	Root      string `json:"root_hash"`
	Records   int    `json:"records"`
	Bytes     int64  `json:"bytes"`
	WireBytes int64  `json:"wire_bytes"`
}

type entryDTO struct {
	Key     string     `json:"key"`
	Value   []byte     `json:"value,omitempty"`
	Ver     versionDTO `json:"ver"`
	Origin  string     `json:"origin"`
	Deleted bool       `json:"deleted,omitempty"`
}

type versionDTO struct {
	Wall uint64 `json:"wall"`
	Log  uint64 `json:"log"`
}

func dtoEntry(e store.Entry) entryDTO {
	return entryDTO{
		Key: e.Key, Value: e.Value,
		Ver:    versionDTO{Wall: e.Ver.Wall, Log: e.Ver.Log},
		Origin: e.Origin, Deleted: e.Deleted,
	}
}

func entryFromDTO(d entryDTO) store.Entry {
	return store.Entry{
		Key: d.Key, Value: d.Value,
		Ver:    hlc.Timestamp{Wall: d.Ver.Wall, Log: d.Ver.Log},
		Origin: d.Origin, Deleted: d.Deleted,
	}
}

type apiError struct {
	status  int
	code    string
	message string
}

func (e *apiError) Error() string {
	return fmt.Sprintf("peer error %d: %s (%s)", e.status, e.code, e.message)
}

type roundAccum struct {
	HashNodesExchanged   int
	LeafHashNodes        int
	InternalHashNodes    int
	BucketsOpened        int
	EntriesExchanged     int
	EntriesFetched       int
	EntriesSent          int
	UserBytesExchanged   int64
	WireBytesExchanged   int64
	BaselineFullTransfer int64
	LocalRoot            string
	RemoteRoot           string
	ConflictReason       string
}

// diffRound performs one consistent-snapshot round. moved==true means a root
// move was detected and the caller must retry.
func (c *Client) diffRound(ctx context.Context) (acc roundAccum, moved bool, err error) {
	p := c.cfg.Params
	c.attempt = 0

	// Phase 1: pin snapshots on both sides.
	localSnap := c.cfg.Local.Snapshot()
	localTree := merkle.Build(p, localSnap)
	acc.LocalRoot = localTree.RootHash()

	var remote snapshotInfo
	if err = c.postJSON(ctx, "/v1/snapshots", nil, &remote, &acc); err != nil {
		return acc, false, err
	}
	acc.RemoteRoot = remote.Root
	// Always try to release the remote snapshot when the round ends.
	defer func() {
		_ = c.postJSON(context.Background(), "/v1/snapshots/"+remote.ID+"/release", nil, new(any), &acc)
	}()

	if acc.LocalRoot == acc.RemoteRoot {
		// Same root means identical content. Both root hashes piggybacked on
		// the snapshot creation responses, so no dedicated node requests were
		// made: the reconciliation cost exactly two snapshot calls.
		acc.BaselineFullTransfer = localSnap.WireBytes + remote.WireBytes
		return acc, false, nil
	}

	// Phase 2: top-down BFS. Queue items: (local path digits, remote path digits).
	type item struct {
		digits []int
		leaf   bool
		bucket int
		lhash  string
		rhash  string
	}
	var queue []item
	queue = append(queue, item{lhash: acc.LocalRoot, rhash: acc.RemoteRoot})

	type bucketPair struct {
		idx  int
		lEnt []store.Entry
		rEnt []store.Entry
	}
	var differing []bucketPair

	fetchPaths := func(items []item) ([]remoteNode, error) {
		paths := make([]string, len(items))
		for i, it := range items {
			paths[i] = merkle.JoinPath(it.digits)
		}
		var resp struct {
			Nodes []remoteNode `json:"nodes"`
			Epoch int64        `json:"epoch"`
		}
		if err := c.postJSON(ctx, "/v1/nodes",
			map[string]any{"snapshot_id": remote.ID, "paths": paths}, &resp, &acc); err != nil {
			return nil, err
		}
		if resp.Epoch != remote.Epoch {
			return nil, errSnapshotMoved
		}
		return resp.Nodes, nil
	}

	for len(queue) > 0 {
		// Split queue into interior nodes (fetch children in batches) and
		// leaves (fetch entries in batches).
		var interior, leaves []item
		for _, it := range queue {
			if it.leaf {
				leaves = append(leaves, it)
			} else {
				interior = append(interior, it)
			}
		}
		queue = nil

		// Interior: pull remote children hashes and compare level-by-level.
		for start := 0; start < len(interior); start += batchPaths {
			end := min(start+batchPaths, len(interior))
			batch := interior[start:end]
			rnodes, err := fetchPaths(batch)
			if err != nil {
				if ae, ok := moveErr(err); ok {
					acc.ConflictReason = "remote_" + ae.code
					return acc, true, nil
				}
				return acc, false, err
			}
			if len(rnodes) != len(batch) {
				return acc, false, fmt.Errorf("peer returned %d nodes for %d paths", len(rnodes), len(batch))
			}
			// Each requested interior node's local children come from the
			// local tree; we fetch one local node per batch item cheaply
			// (in-process) rather than walking ourselves.
			for i, it := range batch {
				rn := rnodes[i]
				if rn.Error != "" {
					return acc, false, fmt.Errorf("peer node %s: %s", rn.Path, rn.Error)
				}
				acc.HashNodesExchanged++
				acc.InternalHashNodes++
				if len(rn.Children) != p.Fanout {
					return acc, false, fmt.Errorf("peer node %s returned %d children, want %d",
						rn.Path, len(rn.Children), p.Fanout)
				}
				for d := 0; d < p.Fanout; d++ {
					childDigits := append(copyDigits(it.digits), d)
					lNode, err := localTree.Node(merkle.JoinPath(childDigits))
					if err != nil {
						return acc, false, err
					}
					if lNode.Hash != rn.Children[d] {
						ni := item{digits: childDigits, lhash: lNode.Hash, rhash: rn.Children[d]}
						if lNode.Leaf {
							ni.leaf = true
							ni.bucket = lNode.Bucket
						}
						queue = append(queue, ni)
					}
				}
			}
		}

		// Leaves: collect differing bucket indices and fetch remote contents.
		var diffIdx []int
		for _, it := range leaves {
			acc.HashNodesExchanged++
			acc.LeafHashNodes++
			if it.lhash != it.rhash {
				diffIdx = append(diffIdx, it.bucket)
			}
		}
		if len(diffIdx) > 0 {
			acc.BucketsOpened += len(diffIdx)
			for start := 0; start < len(diffIdx); start += batchBuckets {
				end := min(start+batchBuckets, len(diffIdx))
				buckets := diffIdx[start:end]
				var resp struct {
					Buckets []struct {
						Bucket  int        `json:"bucket"`
						Entries []entryDTO `json:"entries"`
					} `json:"buckets"`
					Epoch int64 `json:"epoch"`
				}
				if err := c.postJSON(ctx, "/v1/entries",
					map[string]any{"snapshot_id": remote.ID, "buckets": buckets}, &resp, &acc); err != nil {
					if ae, ok := moveErr(err); ok {
						acc.ConflictReason = "remote_" + ae.code
						return acc, true, nil
					}
					return acc, false, err
				}
				if resp.Epoch != remote.Epoch {
					acc.ConflictReason = "remote_snapshot_moved"
					return acc, true, nil
				}
				got := make(map[int][]store.Entry, len(resp.Buckets))
				for _, b := range resp.Buckets {
					es := make([]store.Entry, len(b.Entries))
					for i, d := range b.Entries {
						es[i] = entryFromDTO(d)
					}
					got[b.Bucket] = es
				}
				for _, idx := range buckets {
					differing = append(differing, bucketPair{
						idx:  idx,
						lEnt: localTree.BucketEntries(idx),
						rEnt: got[idx],
					})
				}
			}
		}
	}

	// Test/diagnostic hook: both snapshots still pinned, nothing applied yet.
	if c.cfg.BetweenPhase != nil {
		c.cfg.BetweenPhase(c.round)
	}

	// Phase 3: deterministic per-bucket LWW union merge.
	type bucketPlan struct {
		idx        int
		merged     []store.Entry
		mergedHash string
	}
	plans := make([]bucketPlan, 0, len(differing))
	var toApply, toSend []store.Entry
	fetched := 0
	for _, bp := range differing {
		fetched += len(bp.rEnt)
		merged := mergeEntries(bp.lEnt, bp.rEnt)
		h := merkle.HashLeaf(merged)
		plans = append(plans, bucketPlan{idx: bp.idx, merged: merged, mergedHash: h})
		toApply = append(toApply, missingWinners(bp.lEnt, merged)...)
		toSend = append(toSend, missingWinners(bp.rEnt, merged)...)
	}
	acc.EntriesFetched = fetched
	acc.EntriesSent = len(toSend)
	acc.EntriesExchanged = acc.EntriesFetched + acc.EntriesSent
	acc.UserBytesExchanged = entryBytes(toApply) + entryBytes(toSend)

	// Predicted post-merge root, computed before any data moves.
	leafMap := make(map[int]string, len(plans))
	for _, pl := range plans {
		leafMap[pl.idx] = pl.mergedHash
	}
	expectedRoot, err := localTree.ExpectedRoot(leafMap)
	if err != nil {
		return acc, false, err
	}

	// Phase 4: two-phase apply. Remote first, guarded by its snapshot epoch.
	var remoteApply struct {
		Applied   int    `json:"applied"`
		Unchanged int    `json:"unchanged"`
		Epoch     int64  `json:"epoch"`
		RootHash  string `json:"root_hash"`
		Changed   bool   `json:"changed"`
	}
	sendDTOs := make([]entryDTO, len(toSend))
	for i, e := range toSend {
		sendDTOs[i] = dtoEntry(e)
	}
	err = c.postJSON(ctx, "/v1/apply",
		map[string]any{"expected_epoch": remote.Epoch, "entries": sendDTOs}, &remoteApply, &acc)
	if err != nil {
		var ae *apiError
		if errors.As(err, &ae) && (ae.status == http.StatusConflict || ae.status == http.StatusNotFound) {
			acc.ConflictReason = "remote_" + ae.code
			return acc, true, nil
		}
		return acc, false, err
	}
	if remoteApply.RootHash != expectedRoot {
		// Remote committed something else than the symmetric merge (e.g. a
		// third writer): discard our side and retry.
		acc.ConflictReason = "remote_root_mismatch"
		return acc, true, nil
	}

	// Local apply, guarded by our own snapshot epoch.
	if _, ok := c.cfg.Local.ApplyEntries(localSnap.Epoch, toApply); !ok {
		acc.ConflictReason = "local_epoch_moved"
		return acc, true, nil
	}
	newLocalSnap := c.cfg.Local.Snapshot()
	newLocalTree := merkle.Build(p, newLocalSnap)
	if newLocalTree.RootHash() != expectedRoot {
		acc.ConflictReason = "local_root_mismatch"
		return acc, true, nil
	}

	acc.LocalRoot = newLocalTree.RootHash()
	acc.RemoteRoot = remoteApply.RootHash
	// Baseline = what a naive "ship everything" reconciliation would have
	// moved (JSON-encoded record sets of both sides). Sizes ride along in
	// the snapshot response, so measuring them costs no data transfer.
	acc.BaselineFullTransfer = localSnap.WireBytes + remote.WireBytes
	if acc.LocalRoot != acc.RemoteRoot {
		acc.ConflictReason = "post_apply_roots_differ"
		return acc, true, nil
	}
	return acc, false, nil
}

// remoteNode is the /v1/nodes item shape.
type remoteNode struct {
	Path     string   `json:"path"`
	Level    int      `json:"level"`
	Leaf     bool     `json:"leaf"`
	Hash     string   `json:"hash"`
	Children []string `json:"children"`
	Bucket   int      `json:"bucket"`
	Count    int      `json:"count"`
	Error    string   `json:"error"`
}

func copyDigits(d []int) []int {
	out := make([]int, len(d))
	copy(out, d)
	return out
}

// mergeEntries computes the LWW union of two buckets' winner maps.
func mergeEntries(a, b []store.Entry) []store.Entry {
	winners := make(map[string]store.Entry, len(a)+len(b))
	take := func(es []store.Entry) {
		for _, e := range es {
			cur, ok := winners[e.Key]
			if !ok || winnerIs(e, cur) {
				winners[e.Key] = e
			}
		}
	}
	take(a)
	take(b)
	out := make([]store.Entry, 0, len(winners))
	for _, e := range winners {
		out = append(out, e)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key < out[j].Key })
	return out
}

// winnerIs reports whether a should beat b: higher HLC version, with the
// larger origin string breaking exact timestamp ties. Entry contains []byte
// so the struct itself cannot be compared with ==.
func winnerIs(a, b store.Entry) bool {
	if c := a.Ver.Compare(b.Ver); c != 0 {
		return c > 0
	}
	return a.Origin >= b.Origin
}

// missingWinners returns entries from winners that the current bucket does
// not already hold as the same version (i.e. what this side must receive).
func missingWinners(current []store.Entry, winners []store.Entry) []store.Entry {
	cur := make(map[string]store.Entry, len(current))
	for _, e := range current {
		cur[e.Key] = e
	}
	var out []store.Entry
	for _, w := range winners {
		have, ok := cur[w.Key]
		if !ok {
			out = append(out, w)
			continue
		}
		// Have some other version: send the winner unless ours is already it.
		if !winnerIs(have, w) {
			out = append(out, w)
		}
	}
	return out
}

func entryBytes(es []store.Entry) int64 {
	var n int64
	for _, e := range es {
		n += int64(len(e.Key) + len(e.Value))
	}
	return n
}

// ---- HTTP plumbing --------------------------------------------------------

func (c *Client) postJSON(ctx context.Context, path string, body any, out any, acc *roundAccum) error {
	if c.cfg.BeforePeerRequest != nil {
		c.cfg.BeforePeerRequest(c.attempt, path)
	}
	c.attempt++
	var rdr io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return err
		}
		if acc != nil {
			acc.WireBytesExchanged += int64(len(raw))
		}
		rdr = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.Peer+path, rdr)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.cfg.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxRespBytes))
	if err != nil {
		return err
	}
	if acc != nil {
		acc.WireBytesExchanged += int64(len(data))
	}
	if resp.StatusCode >= 300 {
		var e struct {
			Error   string `json:"error"`
			Message string `json:"message"`
		}
		_ = json.Unmarshal(data, &e)
		return &apiError{status: resp.StatusCode, code: e.Error, message: e.Message}
	}
	if out != nil {
		if err := json.Unmarshal(data, out); err != nil {
			return fmt.Errorf("decoding %s response: %w", path, err)
		}
	}
	return nil
}

const maxRespBytes = 128 << 20

package syncapi

import (
	"context"
	"errors"
	"sort"

	"merklesync/internal/merkle"
	"merklesync/internal/store"
)

// maxAttempts bounds the number of rounds before Sync gives up. Each round
// restarting because the peer root moved counts as one attempt.
const maxAttempts = 20

// ErrNotConverged is returned when rounds keep disagreeing past maxAttempts
// (e.g. a writer committing continuously faster than sync completes).
var ErrNotConverged = errors.New("sync did not converge within the attempt budget (peer is mutating continuously)")

// SyncStats reports one complete Sync() run.
type SyncStats struct {
	Attempts            int    `json:"attempts"`
	Conflicts           int    `json:"conflicts"` // rounds aborted by a 409
	DifferingBuckets    int    `json:"differingBuckets"`
	NodeHashesCompared  int    `json:"nodeHashesCompared"`
	LeafMetasCompared   int    `json:"leafMetasCompared"`
	ValuesPulled        int    `json:"valuesPulled"`
	EntriesPushed       int    `json:"entriesPushed"`
	AppliedLocally      int    `json:"appliedLocally"`
	AppliedRemotely     int    `json:"appliedRemotely"`
	TombstonesExchanged int    `json:"tombstonesExchanged"`
	Converged           bool   `json:"converged"`
	FinalLocalRevision  int64  `json:"finalLocalRevision"`
	FinalPeerRevision   int64  `json:"finalPeerRevision"`
	FinalLocalRoot      string `json:"finalLocalRoot"`
	FinalPeerRoot       string `json:"finalPeerRoot"`
}

// Syncer performs anti-entropy from a local replica toward a peer Client.
// A round is bidirectional: newer records are pulled from the peer and pushed
// to the peer (LWW, higher version wins), so after a quiet period both roots
// are identical.
type Syncer struct {
	Local Local
	Peer  *Client

	// WireStats is the peer client's byte/call counters as of the last Sync.
	WireStats Stats
}

func NewSyncer(local Local, peer *Client) *Syncer {
	return &Syncer{Local: local, Peer: peer}
}

type localView struct {
	snap store.Snapshot
	tree *merkle.Tree
}

func (sy *Syncer) localViewNow() localView {
	snap := sy.Local.Snapshot()
	return localView{snap: snap, tree: merkle.Build(snap)}
}

// runOnce executes a single anti-entropy round against the peer revision
// captured at the start. It returns ErrConflict (via the HTTP layer) if the
// peer mutated during the scan.
func (sy *Syncer) runOnce(ctx context.Context, acc *SyncStats) error {
	lv := sy.localViewNow()

	peerRoot, err := sy.Peer.Root(ctx)
	if err != nil {
		return err
	}
	if peerRoot.Root == lv.tree.Root() {
		acc.FinalLocalRoot = lv.tree.Root()
		acc.FinalPeerRoot = peerRoot.Root
		return nil
	}

	// --- descend the fixed tree, level by level, batching node requests ---
	// Root (level 4) already differs; inspect its two children first.
	// The walk includes level 0 so that a differing inner node does not
	// make us scan an unchanged sibling bucket.
	var buckets []int
	diff := []int{0, 1} // node indices at level 3
	for level := merkle.RootLevel - 1; level >= 0; level-- {
		hashes, err := sy.Peer.Nodes(ctx, peerRoot.Revision, level, diff)
		if err != nil {
			return err
		}
		acc.NodeHashesCompared += len(diff)
		if level == 0 {
			for i, idx := range diff {
				if hashes[i] != "" && hashes[i] != lv.tree.Node(level, idx) {
					buckets = append(buckets, idx)
				}
			}
			break
		}
		next := make([]int, 0, len(diff)*2)
		for i, idx := range diff {
			if hashes[i] != "" && hashes[i] != lv.tree.Node(level, idx) {
				next = append(next, 2*idx, 2*idx+1)
			}
		}
		diff = next
		if len(diff) == 0 {
			break
		}
	}
	acc.DifferingBuckets += len(buckets)
	if len(buckets) == 0 {
		return nil
	}

	// --- compare per-key metadata inside each divergent bucket ---
	peerMetas, err := sy.Peer.Leaves(ctx, peerRoot.Revision, buckets)
	if err != nil {
		return err
	}
	acc.LeafMetasCompared += len(peerMetas)

	peerByKey := make(map[string]EntryMeta, len(peerMetas))
	for _, m := range peerMetas {
		peerByKey[m.Key] = m
	}

	var pullKeys []string // keys whose peer VALUE must be downloaded
	peerPullMetas := map[string]EntryMeta{}
	var pushEntries []store.Entry
	localApplied := 0
	// Map of the local snapshot taken this round, used for the final
	// staleness guard without racing a concurrent local writer.
	localByName := map[string]store.Entry{}
	for _, e := range lv.snap.Entries {
		localByName[e.Key] = e
	}

	applyTombstoneLocal := func(key string, version int64) {
		n := sy.Local.ApplyEntries([]store.Entry{{Key: key, Version: version, Deleted: true}})
		localApplied += n
		if n > 0 {
			acc.TombstonesExchanged += n
		}
	}

	for _, b := range buckets {
		for _, le := range lv.tree.LeafEntries(b) {
			pm, ok := peerByKey[le.Key]
			if !ok {
				// Peer has never seen the key: local record (even a
				// tombstone) must propagate.
				pushEntries = append(pushEntries, le)
				if le.Deleted {
					acc.TombstonesExchanged++
				}
				continue
			}
			switch {
			case pm.Version > le.Version:
				// Peer record is newer: pull it. Tombstones need no value.
				if pm.Deleted {
					applyTombstoneLocal(le.Key, pm.Version)
				} else {
					pullKeys = append(pullKeys, le.Key)
					peerPullMetas[le.Key] = pm
				}
			case le.Version > pm.Version:
				pushEntries = append(pushEntries, le)
				if le.Deleted {
					acc.TombstonesExchanged++
				}
			}
			// equal versions: records must be identical (same hash); skip.
		}
	}
	// Keys the peer knows but the local snapshot does not contain at all.
	for _, pm := range peerMetas {
		if _, ok := localByName[pm.Key]; ok {
			continue
		}
		if pm.Deleted {
			applyTombstoneLocal(pm.Key, pm.Version)
		} else {
			pullKeys = append(pullKeys, pm.Key)
			peerPullMetas[pm.Key] = pm
		}
	}

	// --- fetch only the values we actually need ---
	sort.Strings(pullKeys)
	var pulled []store.Entry
	if len(pullKeys) > 0 {
		pulled, err = sy.Peer.Entries(ctx, peerRoot.Revision, pullKeys)
		if err != nil {
			return err
		}
	}
	acc.ValuesPulled += len(pulled)

	// Guard against a malformed/lying peer: only accept a value if its
	// advertised version is still newer than the local record's.
	accept := make([]store.Entry, 0, len(pulled))
	for _, e := range pulled {
		if pm, ok := peerPullMetas[e.Key]; !ok || pm.Version != e.Version || e.Deleted {
			continue
		}
		if cur, has := localByName[e.Key]; !has || e.Version > cur.Version {
			accept = append(accept, e)
		}
	}
	localApplied += sy.Local.ApplyEntries(accept)
	acc.AppliedLocally += localApplied

	// --- push locally newer records (one batched LWW merge) ---
	if len(pushEntries) > 0 {
		n, err := sy.Peer.Apply(ctx, pushEntries)
		if err != nil {
			return err
		}
		acc.EntriesPushed += len(pushEntries)
		acc.AppliedRemotely += n
	}
	return nil
}

// Sync runs rounds until both replicas report the same root or the attempt
// budget is exhausted.
func (sy *Syncer) Sync(ctx context.Context) (*SyncStats, error) {
	acc := &SyncStats{}
	var lastErr error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		acc.Attempts++
		err := sy.runOnce(ctx, acc)
		var conflict *ErrConflict
		if errors.As(err, &conflict) {
			acc.Conflicts++
			lastErr = err
			continue // peer moved mid-scan: restart from a fresh /root
		}
		if err != nil {
			return acc, err
		}

		// Verify convergence from freshly built trees on both sides.
		lv := sy.localViewNow()
		peerRoot, err := sy.Peer.Root(ctx)
		if err != nil {
			return acc, err
		}
		acc.FinalLocalRoot = lv.tree.Root()
		acc.FinalPeerRoot = peerRoot.Root
		acc.FinalLocalRevision = lv.snap.Revision
		acc.FinalPeerRevision = peerRoot.Revision
		if peerRoot.Root == lv.tree.Root() {
			acc.Converged = true
			sy.WireStats = sy.Peer.Stats()
			return acc, nil
		}
		lastErr = nil
		// Roots still differ without a reported conflict (e.g. a writer
		// committed between the last checked read and verification): retry.
	}
	if lastErr != nil {
		return acc, lastErr
	}
	return acc, ErrNotConverged
}

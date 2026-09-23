// Package syncer implements the interval synchronizer.
//
// Guarantees enforced here:
//
//   - Only the contiguous, cryptographically verified prefix is published;
//     blocks are advanced strictly in order and a gap can never be skipped.
//   - Resume is driven by the durable checkpoint in SQLite, not by any remote
//     node's advertised height. Advertised heights are recorded as evidence
//     but never trusted as progress.
//   - At most MaxParallel segments are in flight at once, and only the narrow
//     window of segments immediately after the verified frontier is fetched,
//     so the out-of-order reorder buffer is bounded by
//     O(MaxParallel * SegmentSize) regardless of chain length.
//   - A bad segment is retried from another node; success from any honest
//     source fills the same range.
//   - After cancellation, results of requests started before the cancel are
//     discarded and the checkpoint is never advanced by them.
package syncer

import (
	"context"
	"errors"
	"fmt"
	"net"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"nodesync/internal/chain"
	"nodesync/internal/store"
)

// Client is the transport the engine needs. The gRPC implementation wraps the
// generated NodeSync client; tests can supply an in-memory fake.
type Client interface {
	NodeID() string
	Status(ctx context.Context) (advertisedHeight int64, advertisedHead, genesis []byte, err error)
	GetSegment(ctx context.Context, start int64, limit int32) ([]chain.Block, error)
}

// Evidence outcomes.
const (
	OutOK                 = "ok"
	OutRPCError           = "rpc_error"
	OutTimeout            = "timeout"
	OutHashMismatch       = "hash_mismatch"
	OutParentMismatch     = "parent_mismatch"
	OutShape              = "shape"
	OutAdvertisedMismatch = "advertised_mismatch"
	OutPublished          = "published"
	OutStaleIgnored       = "stale_ignored"
)

// Config configures a Run.
type Config struct {
	TargetHeight int64         // trusted tip to reach (from the sample)
	GenesisHash  []byte        // trusted genesis hash (from the sample)
	SegmentSize  int32         // blocks per request
	MaxParallel  int           // max concurrent segment fetches
	MaxAttempts  int           // per-segment attempts before giving up
	RPCTimeout   time.Duration // per-request deadline

	// OnStep, if set, is invoked by the coordinator after processing a result
	// (before scheduling more). buffered is the current reorder-buffer size.
	// Test-only observability hook; nil in production.
	OnStep func(inflight, buffered int)
}

// AttemptStat is a per-node fetch tally.
type AttemptStat struct {
	OK        int
	Timeout   int
	BadHash   int
	BadParent int
	RPCError  int
}

// Report summarizes a run.
type Report struct {
	StartCheckpoint   int64
	FinalCheckpoint   int64
	TargetHeight      int64
	Complete          bool
	Attempts          map[string]*AttemptStat
	Failovers         int // retries that used a different node than the prior attempt
	AdvertisedHeights map[string]int64
}

type buffered struct {
	blocks []chain.Block
	nodeID string
}

type result struct {
	start  int64
	end    int64
	client Client
	blocks []chain.Block
	err    error
}

type engine struct {
	cfg      Config
	clients  []Client
	st       *store.Store
	now      func() time.Time
	frontier int64 // next expected (not-yet-published) height

	// All of the following are touched only by the coordinator goroutine.
	buffer    map[int64]buffered // segment start -> internally verified data
	inflight  map[int64]bool
	attempts  map[int64]int // number of launched attempts per start
	lastNode  map[int64]string
	exhausted map[int64]bool
	fatal     error
	stats     map[string]*AttemptStat
	advert    map[string]int64
	failover  int
	nActive   int
	startCP   int64

	results chan result
	wg      sync.WaitGroup
	cancel  context.CancelFunc
}

// Run performs one synchronisation session.
func Run(ctx context.Context, st *store.Store, clients []Client, cfg Config) (*Report, error) {
	if cfg.MaxParallel <= 0 {
		cfg.MaxParallel = 4
	}
	if cfg.MaxAttempts <= 0 {
		cfg.MaxAttempts = len(clients) * 2
	}
	if cfg.SegmentSize <= 0 {
		return nil, errors.New("syncer: SegmentSize must be positive")
	}
	if len(clients) == 0 {
		return nil, errors.New("syncer: no clients")
	}
	if cfg.RPCTimeout <= 0 {
		cfg.RPCTimeout = 2 * time.Second
	}

	cp, cpHash, err := st.Checkpoint(ctx)
	if err != nil {
		return nil, fmt.Errorf("syncer: load checkpoint: %w", err)
	}
	if cp < 0 || cp > cfg.TargetHeight {
		return nil, fmt.Errorf("syncer: checkpoint %d outside [0,%d]", cp, cfg.TargetHeight)
	}
	if len(cpHash) != chain.HashLen {
		return nil, fmt.Errorf("syncer: checkpoint hash length %d invalid", len(cpHash))
	}

	// A fatal per-segment failure cancels in-flight requests and ends the run
	// even if the caller's context is still open.
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	e := &engine{
		cfg:       cfg,
		clients:   clients,
		st:        st,
		now:       time.Now,
		frontier:  cp + 1,
		buffer:    map[int64]buffered{},
		inflight:  map[int64]bool{},
		attempts:  map[int64]int{},
		lastNode:  map[int64]string{},
		exhausted: map[int64]bool{},
		stats:     map[string]*AttemptStat{},
		advert:    map[string]int64{},
		results:   make(chan result),
		cancel:    cancel,
		startCP:   cp,
	}
	for _, c := range clients {
		e.stats[c.NodeID()] = &AttemptStat{}
		e.advert[c.NodeID()] = -1
	}

	if err := e.queryStatus(ctx); err != nil {
		return e.finishReport(err), err
	}

	runErr := e.loop(ctx)
	// A locally-generated fatal failure takes precedence over ctx.Err() when
	// the caller did not cancel.
	if e.fatal != nil && ctx.Err() == nil {
		runErr = e.fatal
	}
	return e.finishReport(runErr), runErr
}

func (e *engine) finishReport(runErr error) *Report {
	final, _, err := e.st.Checkpoint(context.Background())
	if err != nil {
		final = e.startCP
	}
	return &Report{
		StartCheckpoint:   e.startCP,
		FinalCheckpoint:   final,
		TargetHeight:      e.cfg.TargetHeight,
		Complete:          runErr == nil && final >= e.cfg.TargetHeight,
		Attempts:          e.stats,
		Failovers:         e.failover,
		AdvertisedHeights: e.advert,
	}
}

// queryStatus records advertised heights (never trusts them) and checks the
// genesis hash against the trusted sample.
func (e *engine) queryStatus(ctx context.Context) error {
	var anyOK bool
	for _, c := range e.clients {
		cctx, cancel := context.WithTimeout(ctx, e.cfg.RPCTimeout)
		adv, head, genesis, err := c.Status(cctx)
		cancel()
		if err != nil {
			e.stats[c.NodeID()].RPCError++
			_ = e.evidence(ctx, c.NodeID(), 0, 0, 0, OutRPCError, "status: "+err.Error(), -1)
			continue
		}
		anyOK = true
		e.advert[c.NodeID()] = adv
		outcome := OutOK
		detail := fmt.Sprintf("advertised_height=%d trusted_target=%d", adv, e.cfg.TargetHeight)
		if !bytesEqual(genesis, e.cfg.GenesisHash) {
			outcome = OutAdvertisedMismatch
			detail = "genesis hash differs from trusted sample (" + detail + ")"
		} else if adv != e.cfg.TargetHeight {
			outcome = OutAdvertisedMismatch
			detail = "advertised height != trusted target; ignored (" + detail + ")"
		}
		_ = head
		_ = e.evidence(ctx, c.NodeID(), 0, 0, 0, outcome, detail, adv)
	}
	if !anyOK {
		return errors.New("syncer: no node responded to Status")
	}
	return nil
}

// loop runs the single coordinator goroutine. Fetch goroutines only do RPC and
// deliver results; all state mutation happens here.
func (e *engine) loop(ctx context.Context) error {
	for {
		e.fill(ctx)

		if e.fatal != nil {
			e.cancel()
			e.wg.Wait()
			return e.fatal
		}
		if e.frontier > e.cfg.TargetHeight && e.nActive == 0 {
			return nil
		}

		select {
		case <-ctx.Done():
			// Fetch goroutines abort (their RPC ctx is a child of ctx) and drop
			// their result rather than send into a canceled run. Nothing they
			// return can advance the checkpoint.
			e.wg.Wait()
			if e.fatal != nil {
				return e.fatal
			}
			return ctx.Err()

		case r := <-e.results:
			e.nActive--
			// A result that lands after cancellation is stale: never applied.
			if ctx.Err() != nil {
				_ = e.evidence(context.Background(), r.client.NodeID(), r.start, r.end,
					e.attempts[r.start], OutStaleIgnored,
					"delivered after cancel; discarded, checkpoint untouched", -1)
				continue
			}
			e.process(ctx, r)
			if e.cfg.OnStep != nil {
				e.cfg.OnStep(len(e.inflight), len(e.buffer))
			}
			if e.fatal != nil {
				e.cancel()
				e.wg.Wait()
				return e.fatal
			}
			if e.frontier > e.cfg.TargetHeight && e.nActive == 0 {
				return nil
			}
		}
	}
}

// fill launches fetches for the earliest missing segment starts until the
// parallel window is full. The window is counted as in-flight + buffered: a
// buffered-but-not-yet-published segment still occupies a slot, so an
// overtaking later segment can never cause more than MaxParallel segments to
// be outstanding. Starts are run-relative (frontier + k*SegmentSize) so
// segments never overlap, even after an unaligned resume.
func (e *engine) fill(ctx context.Context) {
	for len(e.inflight)+len(e.buffer) < e.cfg.MaxParallel {
		var start int64 = -1
		for h := e.frontier; h <= e.cfg.TargetHeight; h += int64(e.cfg.SegmentSize) {
			if e.exhausted[h] {
				continue
			}
			if _, b := e.buffer[h]; b {
				continue
			}
			if e.inflight[h] {
				continue
			}
			start = h
			break
		}
		if start < 0 {
			return
		}
		client := e.pickNode(start)
		e.inflight[start] = true
		e.attempts[start]++
		if prev := e.lastNode[start]; prev != "" && prev != client.NodeID() {
			e.failover++
		}
		e.lastNode[start] = client.NodeID()
		e.nActive++

		e.wg.Add(1)
		go func(start int64, c Client) {
			defer e.wg.Done()
			cctx, cancel := context.WithTimeout(ctx, e.cfg.RPCTimeout)
			blocks, err := c.GetSegment(cctx, start, e.cfg.SegmentSize)
			cancel()
			end := start + int64(e.cfg.SegmentSize) - 1
			r := result{start: start, end: end, client: c, blocks: blocks, err: err}
			select {
			case e.results <- r:
			case <-ctx.Done():
				// Run canceled: drop rather than risk advancing the checkpoint.
			}
		}(start, client)
	}
}

// pickNode rotates sources with the attempt count so retries use a different
// node than the previous attempt.
func (e *engine) pickNode(start int64) Client {
	n := len(e.clients)
	idx := (int(start) + e.attempts[start]) % n
	if idx < 0 {
		idx += n
	}
	return e.clients[idx]
}

// process validates one fetch, records evidence, buffers good segments, and
// either flushes the contiguous prefix or schedules a retry / fatal failure.
func (e *engine) process(ctx context.Context, r result) {
	delete(e.inflight, r.start)
	st := e.stats[r.client.NodeID()]
	attempt := e.attempts[r.start]
	adv := e.advert[r.client.NodeID()]

	if r.err != nil {
		outcome, detail := classifyError(r.err)
		if outcome == OutTimeout {
			st.Timeout++
		} else {
			st.RPCError++
		}
		_ = e.evidence(ctx, r.client.NodeID(), r.start, r.end, attempt, outcome, detail, adv)
		e.giveUpOrRetry(ctx, r.start)
		return
	}
	if len(r.blocks) == 0 {
		st.RPCError++
		_ = e.evidence(ctx, r.client.NodeID(), r.start, r.end, attempt, OutShape,
			"empty segment before target", adv)
		e.giveUpOrRetry(ctx, r.start)
		return
	}
	if r.blocks[0].Height != r.start {
		st.RPCError++
		_ = e.evidence(ctx, r.client.NodeID(), r.start, r.end, attempt, OutShape,
			fmt.Sprintf("first height %d != requested %d", r.blocks[0].Height, r.start), adv)
		e.giveUpOrRetry(ctx, r.start)
		return
	}
	if err := chain.VerifyInternal(r.blocks); err != nil {
		if errors.Is(err, chain.ErrParentMismatch) {
			st.BadParent++
			_ = e.evidence(ctx, r.client.NodeID(), r.start, r.end, attempt,
				OutParentMismatch, err.Error(), adv)
		} else {
			st.BadHash++
			_ = e.evidence(ctx, r.client.NodeID(), r.start, r.end, attempt,
				OutHashMismatch, err.Error(), adv)
		}
		e.giveUpOrRetry(ctx, r.start)
		return
	}

	// Internally consistent. Buffered pending the trusted-anchor boundary
	// check, which only runs once this segment reaches the verified frontier;
	// a self-consistent fork is therefore never trusted before that point.
	st.OK++
	_ = e.evidence(ctx, r.client.NodeID(), r.start, r.blocks[len(r.blocks)-1].Height,
		attempt, OutOK,
		fmt.Sprintf("hash verified internally, %d blocks buffered pending contiguous anchor", len(r.blocks)),
		adv)
	if _, exists := e.buffer[r.start]; !exists {
		e.buffer[r.start] = buffered{blocks: r.blocks, nodeID: r.client.NodeID()}
	}
	e.publish(ctx)
}

// giveUpOrRetry turns a failed attempt at start into either a fatal error
// (marking the range exhausted so fill stops scheduling it) or leaves it
// missing so fill relaunches from the next source.
func (e *engine) giveUpOrRetry(ctx context.Context, start int64) {
	if e.attempts[start] >= e.cfg.MaxAttempts {
		e.exhausted[start] = true
		e.fatal = fmt.Errorf("syncer: segment at %d failed after %d attempts; unverified gap remains at %d",
			start, e.attempts[start], e.frontier)
	}
}

// publish flushes buffered segments in strict order, performing the
// trusted-anchor check and durably appending each verified segment. It never
// skips a missing start.
func (e *engine) publish(ctx context.Context) {
	for {
		buf, ok := e.buffer[e.frontier]
		if !ok {
			return // gap: stop, do not skip ahead
		}
		seg := buf.blocks
		if e.frontier+int64(len(seg))-1 > e.cfg.TargetHeight {
			cut := e.cfg.TargetHeight - e.frontier + 1
			seg = seg[:cut]
		}
		cp, cpHash, err := e.st.Checkpoint(ctx)
		if err != nil || cp != e.frontier-1 {
			panic(fmt.Sprintf("syncer: checkpoint/frontier desync cp=%d frontier=%d err=%v",
				cp, e.frontier, err))
		}
		if err := chain.VerifyAppend(cpHash, e.frontier, seg); err != nil {
			// Boundary rejection: typically a self-consistent bad-parent fork
			// that passed internal verification but commits to the wrong parent.
			_ = e.evidence(ctx, buf.nodeID, e.frontier, e.frontier+int64(len(seg))-1,
				e.attempts[e.frontier], OutParentMismatch,
				"trusted-anchor boundary check failed: "+err.Error(), e.advert[buf.nodeID])
			delete(e.buffer, e.frontier)
			e.giveUpOrRetry(ctx, e.frontier)
			return
		}
		if err := e.st.AppendVerified(ctx, seg); err != nil {
			// AppendVerified re-verifies contiguity; this must not happen.
			panic("syncer: store rejected verified append: " + err.Error())
		}
		last := seg[len(seg)-1]
		_ = e.evidence(ctx, buf.nodeID, e.frontier, last.Height, 0,
			OutPublished, fmt.Sprintf("published %d contiguous blocks up to %d", len(seg), last.Height),
			e.advert[buf.nodeID])
		delete(e.buffer, e.frontier)
		e.frontier = last.Height + 1
	}
}

func (e *engine) evidence(ctx context.Context, node string, start, end int64, attempt int, outcome, detail string, remote int64) error {
	return e.st.AddEvidence(ctx, store.Evidence{
		AtUnixMs:   e.now().UnixMilli(),
		NodeID:     node,
		Start:      start,
		End:        end,
		Attempt:    attempt,
		Outcome:    outcome,
		Detail:     detail,
		RemoteHeig: remote,
	})
}

// classifyError maps a transport error to an evidence outcome and message.
func classifyError(err error) (outcome, detail string) {
	if err == nil {
		return OutOK, ""
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return OutTimeout, "request timed out: " + err.Error()
	}
	// gRPC status deadline also counts as timeout.
	if st, ok := status.FromError(err); ok {
		if st.Code() == codes.DeadlineExceeded {
			return OutTimeout, "request timed out (grpc DeadlineExceeded): " + st.Message()
		}
		if st.Code() == codes.Canceled || errors.Is(err, context.Canceled) {
			return OutRPCError, "request canceled: " + err.Error()
		}
		return OutRPCError, fmt.Sprintf("grpc %s: %s", st.Code(), st.Message())
	}
	if errors.Is(err, context.Canceled) {
		return OutRPCError, "request canceled: " + err.Error()
	}
	var nerr net.Error
	if errors.As(err, &nerr) && nerr.Timeout() {
		return OutTimeout, "network timeout: " + err.Error()
	}
	return OutRPCError, strings.TrimSpace(err.Error())
}

func bytesEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	var d byte
	for i := range a {
		d |= a[i] ^ b[i]
	}
	return d == 0
}

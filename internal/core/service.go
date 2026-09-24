// Package core implements the resource allocation service: atomic
// all-or-nothing acquisition, the dynamic wait-for graph with cycle
// rejection, priority aging, uncertain-on-timeout semantics, confirmed-stop
// revocation and restart recovery.
//
// Concurrency model: every transaction that reads or mutates allocation
// state first takes the same transaction-scoped advisory lock
// (pg_advisory_xact_lock). This serializes allocator decisions while
// leaving ordinary reads cheap; the critical sections are short,
// single-round-trip SQL operations.
package core

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"deadlockcheck/internal/evidence"
)

// advisoryLockID is the 8 ASCII bytes of "DEADLOCK": 0x444541444C4F434B.
const advisoryLockID int64 = 0x444541444C4F434B

// Service is the allocation service.
type Service struct {
	pool         *pgxpool.Pool
	signer       *evidence.Signer
	defaultAging float64
	now          func() time.Time
}

// NewService wires a service over a pool with an evidence signer.
func NewService(pool *pgxpool.Pool, signer *evidence.Signer, defaultAging float64) *Service {
	return &Service{
		pool:         pool,
		signer:       signer,
		defaultAging: defaultAging,
		now:          func() time.Time { return time.Now().UTC() },
	}
}

// ---------------------------------------------------------------------------
// internal snapshot model
// ---------------------------------------------------------------------------

type taskRow struct {
	id       int64
	label    string
	state    string
	priority int
	aging    float64
}

type waiter struct {
	taskID    int64
	label     string
	state     string
	priority  int
	aging     float64
	requestID int64
	reqKind   string
	createdAt time.Time
	resources []Ref
	effPri    float64
}

type snapshot struct {
	tasks map[int64]*taskRow
	holds map[Ref]struct {
		taskID    int64
		requestID int64
		at        time.Time
	}
	waiters []*waiter
}

func (s *Service) loadSnapshot(ctx context.Context, tx pgx.Tx) (*snapshot, error) {
	snap := &snapshot{tasks: map[int64]*taskRow{}}

	rows, err := tx.Query(ctx,
		`SELECT id, label, state, priority, aging_per_sec
		 FROM tasks
		 WHERE state IN ('waiting','running','uncertain')`)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		t := &taskRow{}
		if err := rows.Scan(&t.id, &t.label, &t.state, &t.priority, &t.aging); err != nil {
			rows.Close()
			return nil, err
		}
		snap.tasks[t.id] = t
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}

	snap.holds = make(map[Ref]struct {
		taskID    int64
		requestID int64
		at        time.Time
	})
	hrows, err := tx.Query(ctx,
		`SELECT kind, name, task_id, request_id, acquired_at FROM holds`)
	if err != nil {
		return nil, err
	}
	for hrows.Next() {
		var r Ref
		var h struct {
			taskID    int64
			requestID int64
			at        time.Time
		}
		if err := hrows.Scan(&r.Kind, &r.Name, &h.taskID, &h.requestID, &h.at); err != nil {
			hrows.Close()
			return nil, err
		}
		snap.holds[r] = h
	}
	hrows.Close()
	if err := hrows.Err(); err != nil {
		return nil, err
	}

	wrows, err := tx.Query(ctx,
		`SELECT r.id, r.task_id, r.kind, r.created_at,
		        t.label, t.state, t.priority, t.aging_per_sec
		 FROM requests r
		 JOIN tasks t ON t.id = r.task_id
		 WHERE r.state = 'waiting' AND t.pending_request_id = r.id`)
	if err != nil {
		return nil, err
	}
	itemByReq := map[int64][]Ref{}
	for wrows.Next() {
		w := &waiter{}
		if err := wrows.Scan(&w.requestID, &w.taskID, &w.reqKind, &w.createdAt,
			&w.label, &w.state, &w.priority, &w.aging); err != nil {
			wrows.Close()
			return nil, err
		}
		itemByReq[w.requestID] = []Ref{}
		snap.waiters = append(snap.waiters, w)
	}
	wrows.Close()
	if err := wrows.Err(); err != nil {
		return nil, err
	}

	if len(itemByReq) > 0 {
		irows, err := tx.Query(ctx,
			`SELECT request_id, kind, name FROM request_items ORDER BY request_id, kind, name`)
		if err != nil {
			return nil, err
		}
		for irows.Next() {
			var reqID int64
			var r Ref
			if err := irows.Scan(&reqID, &r.Kind, &r.Name); err != nil {
				irows.Close()
				return nil, err
			}
			if _, ok := itemByReq[reqID]; ok {
				itemByReq[reqID] = append(itemByReq[reqID], r)
			}
		}
		irows.Close()
		if err := irows.Err(); err != nil {
			return nil, err
		}
	}
	for _, w := range snap.waiters {
		w.resources = itemByReq[w.requestID]
	}

	now := s.now()
	for _, w := range snap.waiters {
		w.effPri = effectivePriority(w.priority, w.aging, w.createdAt, now)
	}
	sort.SliceStable(snap.waiters, func(i, j int) bool {
		a, b := snap.waiters[i], snap.waiters[j]
		if a.effPri != b.effPri {
			return a.effPri > b.effPri
		}
		if !a.createdAt.Equal(b.createdAt) {
			return a.createdAt.Before(b.createdAt)
		}
		return a.requestID < b.requestID
	})
	return snap, nil
}

// effectivePriority = base priority + aging boost for every second waited.
func effectivePriority(base int, agingPerSec float64, waitingSince, now time.Time) float64 {
	secs := now.Sub(waitingSince).Seconds()
	if secs < 0 {
		secs = 0
	}
	return float64(base) + agingPerSec*secs
}

// reconcileEdges rewrites wait_edges to exactly match the snapshot:
// one edge per contended (waiter -> holder) resource class. Must be called
// while holding the advisory lock.
func (s *Service) reconcileEdges(ctx context.Context, tx pgx.Tx, snap *snapshot) error {
	if _, err := tx.Exec(ctx, `TRUNCATE wait_edges`); err != nil {
		return err
	}
	seen := map[[4]any]bool{}
	for _, w := range snap.waiters {
		for _, r := range w.resources {
			h, ok := snap.holds[r]
			if !ok || h.taskID == w.taskID {
				continue
			}
			key := [4]any{w.taskID, h.taskID, r.Kind, r.Name}
			if seen[key] {
				continue
			}
			seen[key] = true
			if _, err := tx.Exec(ctx,
				`INSERT INTO wait_edges
				    (waiter_task_id, holder_task_id, resource_kind, resource_name)
				 VALUES ($1,$2,$3,$4)`,
				w.taskID, h.taskID, r.Kind, r.Name); err != nil {
				return err
			}
		}
	}
	return nil
}

// edgeMap collapses (waiter,holder,resource) edges into a simple graph with
// one adjacency entry per distinct holder.
func edgeMapFromWaiters(snap *snapshot) map[int64]map[int64]bool {
	g := map[int64]map[int64]bool{}
	for _, w := range snap.waiters {
		for _, r := range w.resources {
			h, ok := snap.holds[r]
			if !ok || h.taskID == w.taskID {
				continue
			}
			if g[w.taskID] == nil {
				g[w.taskID] = map[int64]bool{}
			}
			g[w.taskID][h.taskID] = true
		}
	}
	return g
}

// findCycleFrom runs DFS from start and returns the first cycle as the
// sequence of task ids ending with the repeated node again, e.g.
// [3,5,7,3]. Returns nil when start reaches no back-edge.
func findCycleFrom(g map[int64]map[int64]bool, start int64) []int64 {
	const white, gray, black = 0, 1, 2
	color := map[int64]int{}
	var stack []int64
	indexInStack := map[int64]int{}

	var dfs func(n int64) []int64
	dfs = func(n int64) []int64 {
		color[n] = gray
		indexInStack[n] = len(stack)
		stack = append(stack, n)

		holders := make([]int64, 0, len(g[n]))
		for h := range g[n] {
			holders = append(holders, h)
		}
		sort.Slice(holders, func(i, j int) bool { return holders[i] < holders[j] })
		for _, h := range holders {
			switch color[h] {
			case gray:
				cyc := append([]int64{}, stack[indexInStack[h]:]...)
				return append(cyc, h)
			case white:
				if c := dfs(h); c != nil {
					return c
				}
			}
		}
		delete(indexInStack, n)
		stack = stack[:len(stack)-1]
		color[n] = black
		return nil
	}
	return dfs(start)
}

// findAllCycles returns every distinct cycle in the graph, deduplicated by
// the (rotation-insensitive) set of participating nodes. Used by the
// diagnostic endpoint.
func findAllCycles(g map[int64]map[int64]bool) [][]int64 {
	out := [][]int64{}
	seen := map[string]bool{}
	for node := range g {
		c := findCycleFrom(g, node)
		if c == nil {
			continue
		}
		uniq := append([]int64{}, c[:len(c)-1]...)
		sort.Slice(uniq, func(i, j int) bool { return uniq[i] < uniq[j] })
		key := fmt.Sprint(uniq)
		if seen[key] {
			continue
		}
		seen[key] = true
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool { return fmt.Sprint(out[i]) < fmt.Sprint(out[j]) })
	return out
}

// ---------------------------------------------------------------------------
// transaction helper
// ---------------------------------------------------------------------------

// withAllocTx runs fn inside a transaction holding the global advisory lock.
func (s *Service) withAllocTx(ctx context.Context,
	fn func(pgx.Tx) error) (err error) {

	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, advisoryLockID); err != nil {
		return fmt.Errorf("acquire allocator lock: %w", err)
	}
	if err := fn(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// ---------------------------------------------------------------------------
// evidence / event helpers
// ---------------------------------------------------------------------------

func refsToER(rs []Ref) []evidence.Resource {
	out := make([]evidence.Resource, len(rs))
	for i, r := range rs {
		out[i] = evidence.Resource{Kind: r.Kind, Name: r.Name}
	}
	return out
}

// insertEvidenceTx writes a signed allocation-decision record and returns
// its id. The signature is computed in Go with HMAC-SHA256 after the row id
// and timestamp are known, then stored back onto the same row.
func (s *Service) insertEvidenceTx(ctx context.Context, tx pgx.Tx,
	at time.Time, event string, taskID, requestID *int64, rs []Ref) (int64, error) {

	rj, err := evidence.ResourcesJSON(refsToER(rs))
	if err != nil {
		return 0, err
	}
	var id int64
	if err := tx.QueryRow(ctx,
		`INSERT INTO audit_events (at, event, task_id, request_id, resources, canonical, sig)
		 VALUES ($1,$2,$3,$4,$5,'','') RETURNING id`,
		at, event, taskID, requestID, rj).Scan(&id); err != nil {
		return 0, fmt.Errorf("insert audit event: %w", err)
	}
	ev := evidence.Event{
		AuditID:   id,
		At:        at,
		Event:     event,
		TaskID:    taskID,
		RequestID: requestID,
		Resources: refsToER(rs),
	}
	canonical := evidence.Canonical(ev)
	sig := s.signer.Sign(canonical)
	if _, err := tx.Exec(ctx,
		`UPDATE audit_events SET canonical=$1, sig=$2 WHERE id=$3`,
		canonical, sig, id); err != nil {
		return 0, fmt.Errorf("sign audit event %d: %w", id, err)
	}
	return id, nil
}

func (s *Service) insertTaskEventTx(ctx context.Context, tx pgx.Tx,
	taskID int64, event string, detail map[string]any) error {

	if detail == nil {
		detail = map[string]any{}
	}
	dj, _ := json.Marshal(detail)
	_, err := tx.Exec(ctx,
		`INSERT INTO task_events (task_id, event, detail) VALUES ($1,$2,$3)`,
		taskID, event, dj)
	return err
}

// ---------------------------------------------------------------------------
// resource registry
// ---------------------------------------------------------------------------

// RegisterResources upserts tools/stations.
func (s *Service) RegisterResources(ctx context.Context, refs []Ref, descriptions map[Ref]string) error {
	if err := validateRefs(refs); err != nil {
		return err
	}
	return s.withAllocTx(ctx, func(tx pgx.Tx) error {
		for _, r := range refs {
			if r.Kind != "tool" && r.Kind != "station" {
				return errf(ErrValidation, "resource kind must be tool or station, got %q", r.Kind)
			}
			if _, err := tx.Exec(ctx,
				`INSERT INTO resources (kind, name, description)
				 VALUES ($1,$2,$3)
				 ON CONFLICT (kind, name) DO UPDATE SET description = EXCLUDED.description`,
				r.Kind, r.Name, descriptions[r]); err != nil {
				return fmt.Errorf("upsert resource %s/%s: %w", r.Kind, r.Name, err)
			}
		}
		return nil
	})
}

func validateRefs(refs []Ref) error {
	if len(refs) == 0 {
		return errf(ErrValidation, "resources must contain at least one entry")
	}
	seen := map[Ref]bool{}
	for _, r := range refs {
		if r.Kind != "tool" && r.Kind != "station" {
			return errf(ErrValidation, "resource kind must be tool or station, got %q", r.Kind)
		}
		if r.Name == "" {
			return errf(ErrValidation, "resource name must not be empty")
		}
		if seen[r] {
			return errf(ErrValidation, "duplicate resource in request: %s/%s", r.Kind, r.Name)
		}
		seen[r] = true
	}
	return nil
}

// ListResources returns the resource registry with current holder evidence.
func (s *Service) ListResources(ctx context.Context) ([]ResourceView, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT r.kind, r.name, r.description,
		        h.task_id, t.label, h.request_id, h.acquired_at
		 FROM resources r
		 LEFT JOIN holds h ON h.kind = r.kind AND h.name = r.name
		 LEFT JOIN tasks t ON t.id = h.task_id
		 ORDER BY r.kind, r.name`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []ResourceView{}
	for rows.Next() {
		var rv ResourceView
		var (
			tid   *int64
			label *string
			rid   *int64
			acq   *time.Time
		)
		if err := rows.Scan(&rv.Kind, &rv.Name, &rv.Description,
			tid, label, rid, acq); err != nil {
			return nil, err
		}
		if tid != nil {
			rv.HeldBy = &HoldBrief{TaskID: *tid, RequestID: *rid, AcquiredAt: *acq}
			if label != nil {
				rv.HeldBy.TaskLabel = *label
			}
		}
		out = append(out, rv)
	}
	return out, rows.Err()
}

func (s *Service) resourcesExistTx(ctx context.Context, tx pgx.Tx, refs []Ref) error {
	for _, r := range refs {
		var ok bool
		if err := tx.QueryRow(ctx,
			`SELECT EXISTS(SELECT 1 FROM resources WHERE kind=$1 AND name=$2)`,
			r.Kind, r.Name).Scan(&ok); err != nil {
			return err
		}
		if !ok {
			return errf(ErrNotFound, "unknown resource %s/%s", r.Kind, r.Name)
		}
	}
	return nil
}

// ---------------------------------------------------------------------------
// task creation / dynamic requests
// ---------------------------------------------------------------------------

// CreateTask registers a task and atomically attempts its initial request.
func (s *Service) CreateTask(ctx context.Context, in CreateTaskInput) (*AllocationResult, error) {
	if in.Label == "" {
		return nil, errf(ErrValidation, "label is required")
	}
	if err := validateRefs(in.Resources); err != nil {
		return nil, err
	}
	timeout := time.Duration(in.TimeoutMS) * time.Millisecond
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	pri := in.Priority
	if pri == 0 {
		pri = 100
	}
	aging := in.AgingPerSec
	if aging < 0 {
		aging = s.defaultAging
	}

	var taskID, requestID int64
	err := s.withAllocTx(ctx, func(tx pgx.Tx) error {
		if err := s.resourcesExistTx(ctx, tx, in.Resources); err != nil {
			return err
		}
		now := s.now()
		if err := tx.QueryRow(ctx,
			`INSERT INTO tasks (label, priority, aging_per_sec, timeout_ms, state, created_at, updated_at)
			 VALUES ($1,$2,$3,$4,'waiting',$5,$5) RETURNING id`,
			in.Label, pri, aging, timeout.Milliseconds(), now).Scan(&taskID); err != nil {
			return fmt.Errorf("insert task: %w", err)
		}
		if err := tx.QueryRow(ctx,
			`INSERT INTO requests (task_id, kind, state, created_at)
			 VALUES ($1,'initial','waiting',$2) RETURNING id`,
			taskID, now).Scan(&requestID); err != nil {
			return err
		}
		for _, r := range in.Resources {
			if _, err := tx.Exec(ctx,
				`INSERT INTO request_items (request_id, kind, name) VALUES ($1,$2,$3)`,
				requestID, r.Kind, r.Name); err != nil {
				return err
			}
		}
		if _, err := tx.Exec(ctx,
			`UPDATE tasks SET pending_request_id=$1 WHERE id=$2`, requestID, taskID); err != nil {
			return err
		}
		if err := s.insertTaskEventTx(ctx, tx, taskID, "created", map[string]any{
			"request_id": requestID,
			"resources":  refsToER(in.Resources),
			"priority":   pri,
		}); err != nil {
			return err
		}
		return s.runAllocatorPass(ctx, tx)
	})
	if err != nil {
		return nil, mapErr(err)
	}
	return s.describeAllocation(ctx, taskID, requestID)
}

// SubmitExtra is a running task's dynamic request for additional resources.
// It never adds wait-for edges that would close a cycle: such a request is
// rejected with HTTP 409 and leaves existing holds untouched.
func (s *Service) SubmitExtra(ctx context.Context, taskID int64, in ExtraRequestInput) (*AllocationResult, error) {
	if err := validateRefs(in.Resources); err != nil {
		return nil, err
	}
	var requestID int64
	err := s.withAllocTx(ctx, func(tx pgx.Tx) error {
		var state string
		var pending *int64
		err := tx.QueryRow(ctx,
			`SELECT state, pending_request_id FROM tasks WHERE id=$1`, taskID).
			Scan(&state, &pending)
		if errors.Is(err, pgx.ErrNoRows) {
			return errf(ErrNotFound, "task %d not found", taskID)
		}
		if err != nil {
			return err
		}
		if state != "running" {
			return errf(ErrState, "task %d is %s, dynamic requests are only allowed while running", taskID, state)
		}
		if pending != nil {
			return errf(ErrConflict, "task %d already has a pending request %d", taskID, *pending)
		}
		if err := s.resourcesExistTx(ctx, tx, in.Resources); err != nil {
			return err
		}
		for _, r := range in.Resources {
			var holder int64
			err := tx.QueryRow(ctx,
				`SELECT task_id FROM holds WHERE kind=$1 AND name=$2`, r.Kind, r.Name).Scan(&holder)
			if err == nil && holder == taskID {
				return errf(ErrConflict, "task %d already holds %s/%s", taskID, r.Kind, r.Name)
			}
			if err != nil && !errors.Is(err, pgx.ErrNoRows) {
				return err
			}
		}

		now := s.now()
		if err := tx.QueryRow(ctx,
			`INSERT INTO requests (task_id, kind, state, created_at)
			 VALUES ($1,'extra','waiting',$2) RETURNING id`,
			taskID, now).Scan(&requestID); err != nil {
			return err
		}
		for _, r := range in.Resources {
			if _, err := tx.Exec(ctx,
				`INSERT INTO request_items (request_id, kind, name) VALUES ($1,$2,$3)`,
				requestID, r.Kind, r.Name); err != nil {
				return err
			}
		}
		if _, err := tx.Exec(ctx,
			`UPDATE tasks SET pending_request_id=$1, updated_at=$2 WHERE id=$3`,
			requestID, now, taskID); err != nil {
			return err
		}
		if err := s.insertTaskEventTx(ctx, tx, taskID, "extra_requested", map[string]any{
			"request_id": requestID,
			"resources":  refsToER(in.Resources),
		}); err != nil {
			return err
		}

		// Cycle rejection on the hypothetical new graph.
		snap, err := s.loadSnapshot(ctx, tx)
		if err != nil {
			return err
		}
		if cyc := findCycleFrom(edgeMapFromWaiters(snap), taskID); cyc != nil {
			return s.cycleError(ctx, tx, cyc)
		}
		return s.runAllocatorPass(ctx, tx)
	})
	if err != nil {
		return nil, mapErr(err)
	}
	return s.describeAllocation(ctx, taskID, requestID)
}

func (s *Service) cycleError(ctx context.Context, tx pgx.Tx, cyc []int64) error {
	labels := make([]string, len(cyc))
	for i, id := range cyc {
		var label string
		if err := tx.QueryRow(ctx, `SELECT label FROM tasks WHERE id=$1`, id).Scan(&label); err != nil {
			return err
		}
		labels[i] = fmt.Sprintf("%d:%s", id, label)
	}
	return errDetail(ErrCycle,
		"request rejected: granting it would close a wait-for cycle",
		map[string]any{"cycle_task_ids": cyc, "cycle": labels})
}

// runAllocatorPass is the grant wave. It must run inside an allocator
// transaction. It rebuilds edges from a fresh snapshot, then walks waiting
// requests in (aged priority, arrival) order and grants every request whose
// entire resource set is free. Grants are all-or-nothing per request.
func (s *Service) runAllocatorPass(ctx context.Context, tx pgx.Tx) error {
	snap, err := s.loadSnapshot(ctx, tx)
	if err != nil {
		return err
	}
	if err := s.reconcileEdges(ctx, tx, snap); err != nil {
		return err
	}
	if len(snap.waiters) == 0 {
		return nil
	}

	granted := map[Ref]bool{}
	for _, w := range snap.waiters {
		available := true
		for _, r := range w.resources {
			h, busy := snap.holds[r]
			if busy && h.taskID != w.taskID {
				available = false
				break
			}
			if granted[r] {
				available = false
				break
			}
		}
		if !available {
			continue
		}

		now := s.now()
		var evIDs []int64
		for _, r := range w.resources {
			if _, err := tx.Exec(ctx,
				`INSERT INTO holds (kind, name, task_id, request_id, acquired_at)
				 VALUES ($1,$2,$3,$4,$5)`,
				r.Kind, r.Name, w.taskID, w.requestID, now); err != nil {
				return fmt.Errorf("acquire %s/%s: %w", r.Kind, r.Name, err)
			}
			granted[r] = true
		}
		if _, err := tx.Exec(ctx,
			`UPDATE requests SET state='granted', granted_at=$1 WHERE id=$2`,
			now, w.requestID); err != nil {
			return err
		}

		newState := "running"
		if w.state == "uncertain" {
			// An uncertain task that somehow had a waiting request keeps
			// its uncertain lease status.
			newState = "uncertain"
		}
		if w.reqKind == "initial" {
			if _, err := tx.Exec(ctx,
				`UPDATE tasks
				 SET state=$1, pending_request_id=NULL,
				     started_at=COALESCE(started_at,$2),
				     deadline=$2 + make_interval(secs => timeout_ms/1000.0),
				     updated_at=$2
				 WHERE id=$3`,
				newState, now, w.taskID); err != nil {
				return err
			}
		} else {
			if _, err := tx.Exec(ctx,
				`UPDATE tasks SET state=$1, pending_request_id=NULL, updated_at=$2 WHERE id=$3`,
				newState, now, w.taskID); err != nil {
				return err
			}
		}

		tid, rid := w.taskID, w.requestID
		id, err := s.insertEvidenceTx(ctx, tx, now, "granted", &tid, &rid, w.resources)
		if err != nil {
			return err
		}
		evIDs = append(evIDs, id)
		if err := s.insertTaskEventTx(ctx, tx, w.taskID, "granted", map[string]any{
			"request_id":  w.requestID,
			"resources":   refsToER(w.resources),
			"evidence_id": id,
		}); err != nil {
			return err
		}

		// This request is no longer waiting; drop its edges immediately so
		// later requests in the same pass see the post-grant state through
		// both `granted` and the edge table.
		if _, err := tx.Exec(ctx,
			`DELETE FROM wait_edges WHERE waiter_task_id=$1`, w.taskID); err != nil {
			return err
		}
	}
	return nil
}

// describeAllocation builds the API view of one task/request after a pass.
func (s *Service) describeAllocation(ctx context.Context, taskID, requestID int64) (*AllocationResult, error) {
	var out AllocationResult
	err := s.withAllocTx(ctx, func(tx pgx.Tx) error {
		snap, err := s.loadSnapshot(ctx, tx)
		if err != nil {
			return err
		}
		out.TaskID = taskID
		out.RequestID = requestID

		var reqState, reqKind string
		var reqCreated time.Time
		err = tx.QueryRow(ctx,
			`SELECT state, kind, created_at FROM requests WHERE id=$1`, requestID).
			Scan(&reqState, &reqKind, &reqCreated)
		if err != nil {
			return err
		}
		out.Granted = reqState == "granted"

		var state string
		var label string
		var pri int
		var deadline *time.Time
		err = tx.QueryRow(ctx,
			`SELECT state, label, priority, deadline FROM tasks WHERE id=$1`, taskID).
			Scan(&state, &label, &pri, &deadline)
		if err != nil {
			return err
		}
		out.State = state
		out.Deadline = deadline

		held, err := s.holdsOf(ctx, tx, taskID)
		if err != nil {
			return err
		}
		if held == nil {
			held = []Ref{}
		}
		out.HeldResources = held

		if out.Granted {
			acq, err := s.itemsOf(ctx, tx, requestID)
			if err != nil {
				return err
			}
			out.AcquiredResources = acq
		}

		for i, w := range snap.waiters {
			if w.taskID == taskID && w.requestID == requestID {
				out.EffectivePriority = w.effPri
				out.QueuePosition = i + 1
				out.WaitReasons = waitReasons(snap, w)
				return nil
			}
		}
		out.EffectivePriority = float64(pri)
		out.WaitReasons = []WaitReason{}
		return nil
	})
	if err != nil {
		return nil, err
	}
	if err := s.attachEvidenceIDs(ctx, &out, requestID); err != nil {
		return nil, err
	}
	return &out, nil
}

func waitReasons(snap *snapshot, w *waiter) []WaitReason {
	var reasons []WaitReason
	for _, r := range w.resources {
		h, busy := snap.holds[r]
		if !busy || h.taskID == w.taskID {
			continue
		}
		holder := snap.tasks[h.taskID]
		reason := WaitReason{
			Resource:     r,
			HolderTaskID: h.taskID,
			Since:        h.at,
		}
		if holder != nil {
			reason.HolderLabel = holder.label
			reason.HolderState = holder.state
		}
		reasons = append(reasons, reason)
	}
	sort.Slice(reasons, func(i, j int) bool {
		if reasons[i].Resource.Kind != reasons[j].Resource.Kind {
			return reasons[i].Resource.Kind < reasons[j].Resource.Kind
		}
		return reasons[i].Resource.Name < reasons[j].Resource.Name
	})
	return reasons
}

func (s *Service) holdsOf(ctx context.Context, tx pgx.Tx, taskID int64) ([]Ref, error) {
	rows, err := tx.Query(ctx,
		`SELECT kind, name FROM holds WHERE task_id=$1 ORDER BY kind, name`, taskID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Ref
	for rows.Next() {
		var r Ref
		if err := rows.Scan(&r.Kind, &r.Name); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func (s *Service) itemsOf(ctx context.Context, tx pgx.Tx, requestID int64) ([]Ref, error) {
	rows, err := tx.Query(ctx,
		`SELECT kind, name FROM request_items WHERE request_id=$1 ORDER BY kind, name`, requestID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Ref
	for rows.Next() {
		var r Ref
		if err := rows.Scan(&r.Kind, &r.Name); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func (s *Service) attachEvidenceIDs(ctx context.Context, out *AllocationResult, requestID int64) error {
	rows, err := s.pool.Query(ctx,
		`SELECT id FROM audit_events WHERE request_id=$1 AND event='granted' ORDER BY id`, requestID)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return err
		}
		out.EvidenceIDs = append(out.EvidenceIDs, id)
	}
	return rows.Err()
}

// ---------------------------------------------------------------------------
// completion / failure / revocation / heartbeat
// ---------------------------------------------------------------------------

func (s *Service) terminalize(ctx context.Context, taskID int64, terminal, eventName, reason string) (*TaskView, error) {
	err := s.withAllocTx(ctx, func(tx pgx.Tx) error {
		t, err := s.lockTask(ctx, tx, taskID)
		if err != nil {
			return err
		}
		switch t.state {
		case "complete", "failed":
			return errf(ErrState, "task %d is already %s", taskID, t.state)
		case "waiting":
			return errf(ErrState, "task %d has not acquired its resources yet (state=waiting)", taskID)
		}
		// running or uncertain: completion of an uncertain task is the
		// "late completion after timeout" path and is explicitly allowed.

		held, err := s.holdsOf(ctx, tx, taskID)
		if err != nil {
			return err
		}

		if t.pending != nil {
			if _, err := tx.Exec(ctx,
				`UPDATE requests SET state='rejected' WHERE id=$1 AND state='waiting'`, *t.pending); err != nil {
				return err
			}
			items, err := s.itemsOf(ctx, tx, *t.pending)
			if err != nil {
				return err
			}
			rid := *t.pending
			if _, err := s.insertEvidenceTx(ctx, tx, s.now(), "request_rejected", &taskID, &rid, items); err != nil {
				return err
			}
		}

		now := s.now()
		if _, err := tx.Exec(ctx,
			`UPDATE tasks SET state=$1, pending_request_id=NULL, deadline=NULL, updated_at=$2 WHERE id=$3`,
			terminal, now, taskID); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `DELETE FROM holds WHERE task_id=$1`, taskID); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `DELETE FROM wait_edges WHERE waiter_task_id=$1`, taskID); err != nil {
			return err
		}
		if _, err := s.insertEvidenceTx(ctx, tx, now, eventName, &taskID, nil, held); err != nil {
			return err
		}
		detail := map[string]any{"resources": refsToER(held)}
		if reason != "" {
			detail["reason"] = reason
		}
		if err := s.insertTaskEventTx(ctx, tx, taskID, eventName, detail); err != nil {
			return err
		}
		return nil
	})
	if err != nil {
		return nil, mapErr(err)
	}
	// Released resources may unblock waiters.
	if err := s.GrantWave(ctx); err != nil {
		return nil, err
	}
	return s.GetTask(ctx, taskID)
}

// Complete marks a task finished and releases all of its confirmed-stopped
// resources. Works for both running and uncertain (late completion) tasks.
func (s *Service) Complete(ctx context.Context, taskID int64) (*TaskView, error) {
	return s.terminalize(ctx, taskID, "complete", "completed", "")
}

// Fail declares confirmed failure and releases all resources.
func (s *Service) Fail(ctx context.Context, taskID int64, reason string) (*TaskView, error) {
	return s.terminalize(ctx, taskID, "failed", "failed", reason)
}

// Revoke releases only the named resources, and only when the caller asserts
// they have been confirmed stopped. Unknown/not-held resources are reported
// individually with 409; nothing is released on such a failure.
func (s *Service) Revoke(ctx context.Context, taskID int64, in RevokeInput) (*TaskView, error) {
	if err := validateRefs(in.Resources); err != nil {
		return nil, err
	}
	err := s.withAllocTx(ctx, func(tx pgx.Tx) error {
		t, err := s.lockTask(ctx, tx, taskID)
		if err != nil {
			return err
		}
		if t.state != "running" && t.state != "uncertain" {
			return errf(ErrState, "task %d is %s: only running/uncertain tasks can revoke held resources",
				taskID, t.state)
		}
		if err := s.resourcesExistTx(ctx, tx, in.Resources); err != nil {
			return err
		}
		var notHeld []string
		for _, r := range in.Resources {
			var holder int64
			qerr := tx.QueryRow(ctx,
				`SELECT task_id FROM holds WHERE kind=$1 AND name=$2`, r.Kind, r.Name).Scan(&holder)
			if errors.Is(qerr, pgx.ErrNoRows) || (qerr == nil && holder != taskID) {
				notHeld = append(notHeld, r.Kind+"/"+r.Name)
				continue
			}
			if qerr != nil {
				return qerr
			}
		}
		if len(notHeld) > 0 {
			sort.Strings(notHeld)
			return errDetail(ErrNotHeld,
				"revoke refused: every resource must be held by this task and confirmed stopped",
				map[string]any{"not_held": notHeld})
		}

		for _, r := range in.Resources {
			if _, err := tx.Exec(ctx,
				`DELETE FROM holds WHERE kind=$1 AND name=$2 AND task_id=$3`,
				r.Kind, r.Name, taskID); err != nil {
				return err
			}
		}
		now := s.now()
		if _, err := tx.Exec(ctx, `UPDATE tasks SET updated_at=$1 WHERE id=$2`, now, taskID); err != nil {
			return err
		}
		tid := taskID
		if _, err := s.insertEvidenceTx(ctx, tx, now, "revoked", &tid, nil, in.Resources); err != nil {
			return err
		}
		return s.insertTaskEventTx(ctx, tx, taskID, "revoked", map[string]any{
			"resources": refsToER(in.Resources),
			"reason":    in.Reason,
		})
	})
	if err != nil {
		return nil, mapErr(err)
	}
	if err := s.GrantWave(ctx); err != nil {
		return nil, err
	}
	return s.GetTask(ctx, taskID)
}

// Heartbeat extends the lease of a running task and pulls an uncertain task
// (recovered contact after a suspected timeout) back to running.
func (s *Service) Heartbeat(ctx context.Context, taskID int64, in HeartbeatInput) (*TaskView, error) {
	err := s.withAllocTx(ctx, func(tx pgx.Tx) error {
		t, err := s.lockTask(ctx, tx, taskID)
		if err != nil {
			return err
		}
		if t.state != "running" && t.state != "uncertain" {
			return errf(ErrState, "task %d is %s: heartbeat requires running or uncertain", taskID, t.state)
		}
		extend := time.Duration(t.timeoutMS) * time.Millisecond
		if in.ExtendMS != nil && *in.ExtendMS > 0 {
			extend = time.Duration(*in.ExtendMS) * time.Millisecond
		}
		now := s.now()
		if _, err := tx.Exec(ctx,
			`UPDATE tasks SET state='running', deadline=$1, updated_at=$2 WHERE id=$3`,
			now.Add(extend), now, taskID); err != nil {
			return err
		}
		ev := "heartbeat"
		if t.state == "uncertain" {
			ev = "recovered"
		}
		return s.insertTaskEventTx(ctx, tx, taskID, ev, map[string]any{"deadline": now.Add(extend)})
	})
	if err != nil {
		return nil, mapErr(err)
	}
	return s.GetTask(ctx, taskID)
}

type lockedTask struct {
	state     string
	pending   *int64
	timeoutMS int64
}

func (s *Service) lockTask(ctx context.Context, tx pgx.Tx, taskID int64) (*lockedTask, error) {
	t := &lockedTask{}
	err := tx.QueryRow(ctx,
		`SELECT state, pending_request_id, timeout_ms FROM tasks WHERE id=$1 FOR UPDATE`,
		taskID).Scan(&t.state, &t.pending, &t.timeoutMS)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, errf(ErrNotFound, "task %d not found", taskID)
	}
	if err != nil {
		return nil, err
	}
	return t, nil
}

// ---------------------------------------------------------------------------
// timeout sweeper
// ---------------------------------------------------------------------------

// SweepOnce moves every running task whose deadline has passed into the
// uncertain state. Crucially it does NOT release any holds and does NOT run
// a grant wave: the task may still finish, so its resources cannot be handed
// to anyone until stop is confirmed (complete/fail/revoke/heartbeat).
func (s *Service) SweepOnce(ctx context.Context) ([]int64, error) {
	var swept []int64
	err := s.withAllocTx(ctx, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx,
			`SELECT id FROM tasks
			 WHERE state='running' AND deadline IS NOT NULL AND deadline <= $1
			 ORDER BY id FOR UPDATE`, s.now())
		if err != nil {
			return err
		}
		var ids []int64
		for rows.Next() {
			var id int64
			if err := rows.Scan(&id); err != nil {
				rows.Close()
				return err
			}
			ids = append(ids, id)
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return err
		}

		for _, id := range ids {
			held, err := s.holdsOf(ctx, tx, id)
			if err != nil {
				return err
			}
			now := s.now()
			if _, err := tx.Exec(ctx,
				`UPDATE tasks SET state='uncertain', deadline=NULL, updated_at=$1 WHERE id=$2`,
				now, id); err != nil {
				return err
			}
			tid := id
			// Signed proof that these resources are intentionally kept
			// reserved while the task's fate is unknown.
			if _, err := s.insertEvidenceTx(ctx, tx, now, "timed_out", &tid, nil, held); err != nil {
				return err
			}
			if err := s.insertTaskEventTx(ctx, tx, id, "timed_out", map[string]any{
				"resources":            refsToER(held),
				"resources_kept_until": "confirmed stop or recovery",
			}); err != nil {
				return err
			}
			swept = append(swept, id)
		}
		return nil
	})
	if err != nil {
		return nil, mapErr(err)
	}
	return swept, nil
}

// GrantWave exposes the allocator pass (used after releases, recovery and
// from the debug trigger).
func (s *Service) GrantWave(ctx context.Context) error {
	return mapErr(s.withAllocTx(ctx, func(tx pgx.Tx) error {
		return s.runAllocatorPass(ctx, tx)
	}))
}

// RunSweeper periodically executes SweepOnce until the context is cancelled.
func (s *Service) RunSweeper(ctx context.Context, interval time.Duration, onSweep func([]int64)) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			ids, err := s.SweepOnce(ctx)
			if err != nil && ctx.Err() == nil {
				continue
			}
			if len(ids) > 0 && onSweep != nil {
				onSweep(ids)
			}
		}
	}
}

// ---------------------------------------------------------------------------
// queries
// ---------------------------------------------------------------------------

// GetTask returns the full task view including held resources and wait info.
func (s *Service) GetTask(ctx context.Context, taskID int64) (*TaskView, error) {
	var v TaskView
	err := s.withAllocTx(ctx, func(tx pgx.Tx) error {
		var pending *int64
		err := tx.QueryRow(ctx,
			`SELECT id, label, state, priority, aging_per_sec, timeout_ms,
			        pending_request_id, deadline, created_at, started_at, updated_at
			 FROM tasks WHERE id=$1`, taskID).
			Scan(&v.ID, &v.Label, &v.State, &v.Priority, &v.AgingPerSec, &v.TimeoutMS,
				&pending, &v.Deadline, &v.CreatedAt, &v.StartedAt, &v.UpdatedAt)
		if errors.Is(err, pgx.ErrNoRows) {
			return errf(ErrNotFound, "task %d not found", taskID)
		}
		if err != nil {
			return err
		}
		v.PendingRequestID = pending
		held, err := s.holdsOf(ctx, tx, taskID)
		if err != nil {
			return err
		}
		v.HeldResources = held
		if held == nil {
			v.HeldResources = []Ref{}
		}

		snap, err := s.loadSnapshot(ctx, tx)
		if err != nil {
			return err
		}
		for i, w := range snap.waiters {
			if w.taskID == taskID {
				v.EffectivePriority = w.effPri
				v.QueuePosition = i + 1
				v.PendingRequestKind = w.reqKind
				v.WaitReasons = waitReasons(snap, w)
				if v.WaitReasons == nil {
					v.WaitReasons = []WaitReason{}
				}
				return nil
			}
		}
		v.EffectivePriority = float64(v.Priority)
		v.WaitReasons = []WaitReason{}
		return nil
	})
	if err != nil {
		return nil, mapErr(err)
	}
	return &v, nil
}

// ListTasks returns all tasks, newest first.
func (s *Service) ListTasks(ctx context.Context, limit int) ([]*TaskView, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	rows, err := s.pool.Query(ctx,
		`SELECT id FROM tasks ORDER BY id DESC LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	var ids []int64
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return nil, err
		}
		ids = append(ids, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	out := make([]*TaskView, 0, len(ids))
	for _, id := range ids {
		v, err := s.GetTask(ctx, id)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, nil
}

// ListHolds returns every current resource holder — the "who holds what"
// evidence used to audit blocking.
func (s *Service) ListHolds(ctx context.Context) ([]HoldView, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT h.kind, h.name, h.task_id, t.label, h.request_id, h.acquired_at
		 FROM holds h JOIN tasks t ON t.id = h.task_id
		 ORDER BY h.kind, h.name`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []HoldView{}
	for rows.Next() {
		var h HoldView
		if err := rows.Scan(&h.Resource.Kind, &h.Resource.Name, &h.TaskID,
			&h.TaskLabel, &h.RequestID, &h.AcquiredAt); err != nil {
			return nil, err
		}
		out = append(out, h)
	}
	return out, rows.Err()
}

// ListEvents returns the lifecycle event trail of a task.
func (s *Service) ListEvents(ctx context.Context, taskID int64) ([]EventView, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id, at, event, detail FROM task_events
		 WHERE task_id=$1 ORDER BY id`, taskID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []EventView{}
	for rows.Next() {
		var e EventView
		var raw []byte
		if err := rows.Scan(&e.ID, &e.At, &e.Event, &raw); err != nil {
			return nil, err
		}
		_ = json.Unmarshal(raw, &e.Detail)
		out = append(out, e)
	}
	return out, rows.Err()
}

// WaitGraph returns the live dependency graph and any cycles.
func (s *Service) WaitGraph(ctx context.Context) (*WaitGraph, error) {
	g := &WaitGraph{Edges: []Edge{}, Cycles: [][]int64{}}
	err := s.withAllocTx(ctx, func(tx pgx.Tx) error {
		snap, err := s.loadSnapshot(ctx, tx)
		if err != nil {
			return err
		}
		if err := s.reconcileEdges(ctx, tx, snap); err != nil {
			return err
		}
		rows, err := tx.Query(ctx,
			`SELECT we.waiter_task_id, wt.label, we.holder_task_id, ht.label,
			        we.resource_kind, we.resource_name
			 FROM wait_edges we
			 JOIN tasks wt ON wt.id = we.waiter_task_id
			 JOIN tasks ht ON ht.id = we.holder_task_id
			 ORDER BY we.waiter_task_id, we.holder_task_id,
			          we.resource_kind, we.resource_name`)
		if err != nil {
			return err
		}
		for rows.Next() {
			var e Edge
			if err := rows.Scan(&e.WaiterTaskID, &e.WaiterLabel,
				&e.HolderTaskID, &e.HolderLabel,
				&e.Resource.Kind, &e.Resource.Name); err != nil {
				rows.Close()
				return err
			}
			g.Edges = append(g.Edges, e)
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return err
		}
		g.Cycles = findAllCycles(edgeMapFromWaiters(snap))
		return nil
	})
	if err != nil {
		return nil, mapErr(err)
	}
	return g, nil
}

// ---------------------------------------------------------------------------
// evidence ledger
// ---------------------------------------------------------------------------

// ListEvidence returns signed audit records, newest first, optionally scoped
// to a task. Every record is re-verified against the HMAC key.
func (s *Service) ListEvidence(ctx context.Context, taskID *int64, limit int) ([]EvidenceView, error) {
	if limit <= 0 || limit > 1000 {
		limit = 200
	}
	var (
		rows pgx.Rows
		err  error
	)
	if taskID != nil {
		rows, err = s.pool.Query(ctx,
			`SELECT id, at, event, task_id, request_id, resources, canonical, sig
			 FROM audit_events WHERE task_id=$1 ORDER BY id DESC LIMIT $2`, *taskID, limit)
	} else {
		rows, err = s.pool.Query(ctx,
			`SELECT id, at, event, task_id, request_id, resources, canonical, sig
			 FROM audit_events ORDER BY id DESC LIMIT $1`, limit)
	}
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := []EvidenceView{}
	for rows.Next() {
		var (
			e   EvidenceView
			raw []byte
			tid *int64
			rid *int64
		)
		if err := rows.Scan(&e.ID, &e.At, &e.Event, &tid, &rid, &raw,
			&e.Canonical, &e.Signature); err != nil {
			return nil, err
		}
		e.TaskID, e.RequestID = tid, rid
		var ers []evidence.Resource
		if err := json.Unmarshal(raw, &ers); err != nil {
			return nil, err
		}
		e.Resources = make([]Ref, len(ers))
		for i, x := range ers {
			e.Resources[i] = Ref{Kind: x.Kind, Name: x.Name}
		}
		e.Valid = s.signer.Verify(e.Canonical, e.Signature)
		out = append(out, e)
	}
	return out, rows.Err()
}

// ---------------------------------------------------------------------------
// restart recovery
// ---------------------------------------------------------------------------

// Recover is invoked once at startup. All allocation state is persistent, so
// recovery consists of rebuilding the wait-for graph from holds and pending
// requests and reporting what it found. Tasks left "running" keep their
// deadlines; tasks already "uncertain" stay fenced off until contact or
// confirmed stop.
func (s *Service) Recover(ctx context.Context) (map[string]int, error) {
	stats := map[string]int{}
	err := s.withAllocTx(ctx, func(tx pgx.Tx) error {
		snap, err := s.loadSnapshot(ctx, tx)
		if err != nil {
			return err
		}
		if err := s.reconcileEdges(ctx, tx, snap); err != nil {
			return err
		}
		for _, t := range snap.tasks {
			stats[t.state]++
		}
		return nil
	})
	if err != nil {
		return nil, mapErr(err)
	}
	return stats, nil
}

func mapErr(err error) error {
	var ce *Error
	if errors.As(err, &ce) {
		return ce
	}
	return err
}

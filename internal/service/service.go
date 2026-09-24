// Package service implements atomic all-or-nothing resource allocation,
// the dynamic wait-for graph with cycle rejection, priority aging,
// timeout fencing, revocation handshake and restart recovery.
package service

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"deadlockcheck/internal/store"
)

// States a task can be in.
const (
	StateWaiting   = "waiting"
	StateRunning   = "running"
	StateUncertain = "uncertain"
	StateRevoking  = "revoking"
	StateCompleted = "completed"
	StateFailed    = "failed"
	StateRevoked   = "revoked"
)

// ErrRejected marks a business-level rejection (bad state, stale fence...).
type ErrRejected struct{ Msg string }

func (e *ErrRejected) Error() string { return e.Msg }

// Is lets errors.Is(err, ErrRejected) succeed against the typed pointer.
func (e *ErrRejected) Is(target error) bool {
	_, ok := target.(*ErrRejected)
	return ok
}

func reject(format string, a ...any) error { return &ErrRejected{Msg: fmt.Sprintf(format, a...)} }

// Config tunes aging and leases.
type Config struct {
	AgingStep         time.Duration // waiting time per bonus point
	AgingBonusPerStep int           // priority points subtracted per step (lower = more urgent)
	AgingCap          int           // maximum aging bonus
	DefaultDeadline   time.Duration
}

func DefaultConfig() Config {
	return Config{
		AgingStep:         time.Second,
		AgingBonusPerStep: 1,
		AgingCap:          1000,
		DefaultDeadline:   30 * time.Second,
	}
}

// Service holds the dependencies of the allocation core.
type Service struct {
	pool     *pgxpool.Pool
	priv     ed25519.PrivateKey
	pub      ed25519.PublicKey
	serverID string
	now      func() time.Time
	Cfg      Config
}

// New loads signing key and server identity from the database.
func New(ctx context.Context, st *store.Store, cfg Config) (*Service, error) {
	priv, pub, err := st.EvidenceKey(ctx)
	if err != nil {
		return nil, err
	}
	var serverID string
	err = st.Pool.QueryRow(ctx, `SELECT value FROM meta WHERE key='server_id'`).Scan(&serverID)
	if errors.Is(err, pgx.ErrNoRows) {
		buf := make([]byte, 16)
		if _, err := rand.Read(buf); err != nil {
			return nil, err
		}
		serverID = hex.EncodeToString(buf)
		if _, err := st.Pool.Exec(ctx,
			`INSERT INTO meta(key,value) VALUES('server_id',$1)
			 ON CONFLICT (key) DO NOTHING`, serverID); err != nil {
			return nil, fmt.Errorf("server id: %w", err)
		}
	} else if err != nil {
		return nil, fmt.Errorf("server id: %w", err)
	}
	svc := &Service{
		pool: st.Pool, priv: priv, pub: pub, serverID: serverID,
		now: time.Now, Cfg: cfg,
	}
	if err := svc.ensureEpoch(ctx); err != nil {
		return nil, err
	}
	return svc, nil
}

// ensureEpoch makes sure a fence epoch exists from the very first request,
// even before a restart-recovery bump takes place.
func (s *Service) ensureEpoch(ctx context.Context) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO meta(key,value) VALUES('fence_epoch','1') ON CONFLICT DO NOTHING`)
	return err
}

// SetClock overrides time source (tests).
func (s *Service) SetClock(f func() time.Time) { s.now = f }

// PublicKey returns the Ed25519 verify key for evidence auditing.
func (s *Service) PublicKey() ed25519.PublicKey { return s.pub }

// ---------------------------------------------------------------- models

type Resource struct {
	ID          string    `json:"id"`
	Kind        string    `json:"kind"`
	Description string    `json:"description"`
	CreatedAt   time.Time `json:"createdAt"`
}

type HoldEvidence struct {
	ResourceID string    `json:"resourceId"`
	GrantedAt  time.Time `json:"grantedAt"`
	FenceEpoch int64     `json:"fenceEpoch"`
	Token      string    `json:"evidenceToken"`
}

type Task struct {
	ID                string     `json:"id"`
	Priority          int        `json:"priority"`
	State             string     `json:"state"`
	DeadlineMs        int        `json:"deadlineMs"`
	AcquiredAt        *time.Time `json:"acquiredAt,omitempty"`
	LeaseExpires      *time.Time `json:"leaseExpires,omitempty"`
	EnqueuedAt        time.Time  `json:"enqueuedAt"`
	FenceEpoch        int64      `json:"fenceEpoch"`
	LastError         string     `json:"lastError,omitempty"`
	EffectivePriority int        `json:"effectivePriority"`
	CreatedAt         time.Time  `json:"createdAt"`
	UpdatedAt         time.Time  `json:"updatedAt"`
}

type WaitReason struct {
	ResourceID    string    `json:"resourceId"`
	BlockedByTask string    `json:"blockedByTask"`
	HolderState   string    `json:"holderState"`
	Since         time.Time `json:"since"`
	Note          string    `json:"note,omitempty"`
}

type Cycle struct {
	Tasks     []string `json:"tasks"`
	Resources []string `json:"resources"`
}

// TaskDetail is the full status view: state, holds with signed evidence, waits.
type TaskDetail struct {
	Task        Task           `json:"task"`
	Holds       []HoldEvidence `json:"holds"`
	WaitReasons []WaitReason   `json:"waitReasons"`
}

type AcquireResult struct {
	Status      string         `json:"status"` // granted | waiting | rejected
	Task        Task           `json:"task"`
	Holds       []HoldEvidence `json:"holds,omitempty"`
	WaitReasons []WaitReason   `json:"waitReasons,omitempty"`
	Cycles      []Cycle        `json:"cycles,omitempty"`
}

type GraphView struct {
	Nodes  []GraphNode `json:"nodes"`
	Edges  []GraphEdge `json:"edges"`
	Cycles []Cycle     `json:"cycles"`
}
type GraphNode struct {
	TaskID   string   `json:"taskId"`
	State    string   `json:"state"`
	Priority int      `json:"priority"`
	Holds    []string `json:"holds"`
	Wants    []string `json:"wants"`
}
type GraphEdge struct {
	WaiterTask   string `json:"waiterTask"`
	HolderTask   string `json:"holderTask"`
	ResourceID   string `json:"resourceId"`
	ResourceKind string `json:"resourceKind"`
}

// ---------------------------------------------------------------- requests

type RegisterResourceReq struct {
	ID          string `json:"id"`
	Kind        string `json:"kind"`
	Description string `json:"description"`
}

type CreateTaskReq struct {
	ID         string   `json:"id"`
	Priority   int      `json:"priority"`
	Resources  []string `json:"resources"`
	DeadlineMs int      `json:"deadlineMs"`
}

type AddResourcesReq struct {
	TaskID    string   `json:"taskId"`
	Resources []string `json:"resources"`
}

type FenceReq struct {
	FenceEpoch int64 `json:"fenceEpoch"`
}

// ------------------------------------------------------------- resources

func (s *Service) RegisterResource(ctx context.Context, req RegisterResourceReq) (*Resource, error) {
	if req.ID == "" {
		return nil, reject("resource id required")
	}
	if req.Kind != "tool" && req.Kind != "station" {
		return nil, reject("kind must be tool or station")
	}
	r := &Resource{}
	err := s.pool.QueryRow(ctx,
		`INSERT INTO resources(id,kind,description) VALUES($1,$2,$3)
		 ON CONFLICT (id) DO UPDATE SET description=EXCLUDED.description
		 RETURNING id,kind,description,created_at`,
		req.ID, req.Kind, req.Description).
		Scan(&r.ID, &r.Kind, &r.Description, &r.CreatedAt)
	if err != nil {
		return nil, err
	}
	return r, nil
}

func (s *Service) ListResources(ctx context.Context) ([]Resource, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id,kind,description,created_at FROM resources ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Resource
	for rows.Next() {
		var r Resource
		if err := rows.Scan(&r.ID, &r.Kind, &r.Description, &r.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// ------------------------------------------------------------- evidence

type evidencePayload struct {
	Claim      string    `json:"claim"`
	TaskID     string    `json:"taskId"`
	ResourceID string    `json:"resourceId"`
	GrantedAt  time.Time `json:"grantedAt"`
	FenceEpoch int64     `json:"fenceEpoch"`
	ServerID   string    `json:"serverId"`
}

func (s *Service) signEvidence(p evidencePayload) (string, error) {
	raw, err := json.Marshal(p)
	if err != nil {
		return "", err
	}
	sig := ed25519.Sign(s.priv, raw)
	return base64.RawURLEncoding.EncodeToString(raw) + "." +
		base64.RawURLEncoding.EncodeToString(sig), nil
}

// VerifyToken checks a holding-evidence token signature and returns its payload.
func (s *Service) VerifyToken(token string) (*evidencePayload, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 2 {
		return nil, reject("malformed evidence token")
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return nil, reject("malformed token payload")
	}
	sig, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, reject("malformed token signature")
	}
	if !ed25519.Verify(s.pub, raw, sig) {
		return nil, reject("invalid evidence signature")
	}
	var p evidencePayload
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, reject("invalid token body")
	}
	return &p, nil
}

// ----------------------------------------------------------------- tx utils

// allocatorLock serializes allocation decisions so that two concurrent
// requests can never observe the same free resource and both grant it.
const allocatorLock int64 = 680680680

func lockAllocator(ctx context.Context, tx pgx.Tx) error {
	_, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, allocatorLock)
	return err
}

func (s *Service) logEvent(ctx context.Context, tx pgx.Tx, taskID, event, detail string) {
	_, _ = tx.Exec(ctx,
		`INSERT INTO task_events(task_id,event,detail) VALUES($1,$2,$3)`,
		taskID, event, detail)
}

func (s *Service) resourcesExist(ctx context.Context, tx pgx.Tx, ids []string) error {
	if len(ids) == 0 {
		return reject("at least one resource is required")
	}
	var n int
	if err := tx.QueryRow(ctx,
		`SELECT count(*) FROM resources WHERE id = ANY($1)`, ids).Scan(&n); err != nil {
		return err
	}
	if n != len(dedup(ids)) {
		return reject("one or more resources do not exist: %v", ids)
	}
	return nil
}

func dedup(in []string) []string {
	seen := map[string]struct{}{}
	var out []string
	for _, v := range in {
		if _, ok := seen[v]; ok != true {
			seen[v] = struct{}{}
			out = append(out, v)
		}
	}
	return out
}

// cycleFrom searches the wait-for graph (edges: wanted row -> current held
// row on the same resource) for a directed cycle reachable from startTask.
func (s *Service) cycleFrom(ctx context.Context, tx pgx.Tx, startTask string) ([]Cycle, error) {
	rows, err := tx.Query(ctx, `
		WITH RECURSIVE edges AS (
			SELECT w.task_id AS from_task, h.task_id AS to_task, w.resource_id
			FROM task_resources w
			JOIN task_resources h
			  ON h.resource_id = w.resource_id AND h.status='held'
			 AND h.task_id <> w.task_id
			WHERE w.status='wanted'
		),
		walk AS (
			SELECT from_task AS start_node,
			       from_task, to_task,
			       ARRAY[from_task]::text[] AS path,
			       ARRAY[resource_id]::text[] AS rpath
			FROM edges
			WHERE from_task = $1
			UNION ALL
			SELECT w.start_node, e.from_task, e.to_task,
			       w.path || e.from_task,
			       w.rpath || e.resource_id
			FROM walk w
			JOIN edges e ON e.from_task = w.to_task
			WHERE NOT e.from_task = ANY(w.path)
		)
		SELECT path || to_task, rpath
		FROM walk
		WHERE to_task = $1`, startTask)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var cycles []Cycle
	for rows.Next() {
		var c Cycle
		if err := rows.Scan(&c.Tasks, &c.Resources); err != nil {
			return nil, err
		}
		cycles = append(cycles, c)
	}
	return cycles, rows.Err()
}

// waitReasons computes why a task cannot currently get its wanted resources.
func (s *Service) waitReasons(ctx context.Context, tx pgx.Tx, taskID string) ([]WaitReason, error) {
	rows, err := tx.Query(ctx, `
		SELECT w.resource_id, h.task_id, th.state, h.granted_at
		FROM task_resources w
		LEFT JOIN task_resources h
		  ON h.resource_id = w.resource_id AND h.status='held'
		 AND h.task_id <> w.task_id
		LEFT JOIN tasks th ON th.id = h.task_id
		WHERE w.task_id = $1 AND w.status='wanted'
		ORDER BY w.resource_id`, taskID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var reasons []WaitReason
	for rows.Next() {
		var r WaitReason
		var holder *string
		var holderState *string
		var grantedAt *time.Time
		if err := rows.Scan(&r.ResourceID, &holder, &holderState, &grantedAt); err != nil {
			return nil, err
		}
		if holder == nil {
			r.Note = "free; awaiting scheduler promotion"
			reasons = append(reasons, r)
			continue
		}
		r.BlockedByTask = *holder
		r.HolderState = *holderState
		if grantedAt != nil {
			r.Since = *grantedAt
		}
		if *holderState == StateUncertain {
			r.Note = "holder timed out; resource is fenced in uncertain state and cannot be reassigned until the holder is confirmed stopped"
		} else if *holderState == StateRevoking {
			r.Note = "holder revocation requested but stop not yet confirmed; resource still held"
		}
		reasons = append(reasons, r)
	}
	return reasons, rows.Err()
}

func scanTask(row pgx.Row) (Task, error) {
	var t Task
	var acquired, lease *time.Time
	err := row.Scan(&t.ID, &t.Priority, &t.State, &t.DeadlineMs,
		&acquired, &lease, &t.EnqueuedAt, &t.FenceEpoch, &t.LastError,
		&t.CreatedAt, &t.UpdatedAt)
	t.AcquiredAt = acquired
	t.LeaseExpires = lease
	return t, err
}

const taskCols = `id,priority,state,deadline_ms,acquired_at,lease_expires,
	enqueued_at,fence_epoch,last_error,created_at,updated_at`

func (s *Service) loadTask(ctx context.Context, tx pgx.Tx, id string, forUpdate bool) (Task, error) {
	q := `SELECT ` + taskCols + ` FROM tasks WHERE id=$1`
	if forUpdate {
		q += ` FOR UPDATE`
	}
	t, err := scanTask(tx.QueryRow(ctx, q, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return t, reject("task %q not found", id)
	}
	return t, err
}

func (s *Service) withAging(t Task) Task {
	t.EffectivePriority = t.Priority
	if t.State == StateWaiting {
		bonus := int(s.now().Sub(t.EnqueuedAt) / s.Cfg.AgingStep)
		if bonus > s.Cfg.AgingCap {
			bonus = s.Cfg.AgingCap
		}
		t.EffectivePriority = t.Priority - bonus*s.Cfg.AgingBonusPerStep
	}
	return t
}

// holdsFor returns the latest signed grant evidence per held resource.
func (s *Service) holdsFor(ctx context.Context, tx pgx.Tx, taskID string) ([]HoldEvidence, error) {
	rows, err := tx.Query(ctx, `
		SELECT DISTINCT ON (resource_id) resource_id, occurred_at, fence_epoch, evidence
		FROM hold_ledger
		WHERE task_id=$1 AND action='granted'
		ORDER BY resource_id, occurred_at DESC, id DESC`, taskID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []HoldEvidence
	for rows.Next() {
		var h HoldEvidence
		if err := rows.Scan(&h.ResourceID, &h.GrantedAt, &h.FenceEpoch, &h.Token); err != nil {
			return nil, err
		}
		out = append(out, h)
	}
	return out, rows.Err()
}

// ------------------------------------------------------------ create/acquire

// CreateTask atomically attempts to acquire the whole declared resource set.
// It never leaves a partial acquisition behind: either every wanted row is
// granted at once, or the task waits (or is rejected for a would-be cycle).
func (s *Service) CreateTask(ctx context.Context, req CreateTaskReq) (*AcquireResult, error) {
	if req.ID == "" {
		return nil, reject("task id required")
	}
	req.Resources = dedup(req.Resources)
	prio := req.Priority
	deadline := s.Cfg.DefaultDeadline
	if req.DeadlineMs > 0 {
		deadline = time.Duration(req.DeadlineMs) * time.Millisecond
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)

	if err := lockAllocator(ctx, tx); err != nil {
		return nil, err
	}
	if err := s.resourcesExist(ctx, tx, req.Resources); err != nil {
		return nil, err
	}

	var exists bool
	if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM tasks WHERE id=$1)`, req.ID).Scan(&exists); err != nil {
		return nil, err
	}
	if exists {
		return nil, reject("task %q already exists", req.ID)
	}

	now := s.now()
	var epoch int64
	if err := tx.QueryRow(ctx,
		`SELECT COALESCE((SELECT value::bigint FROM meta WHERE key='fence_epoch'),0)`).Scan(&epoch); err != nil {
		return nil, err
	}
	if _, err := tx.Exec(ctx,
		`INSERT INTO tasks(id,priority,state,deadline_ms,enqueued_at,fence_epoch)
		 VALUES($1,$2,'waiting',$3,$4,$5)`,
		req.ID, prio, deadline.Milliseconds(), now, epoch); err != nil {
		return nil, err
	}
	for _, rid := range req.Resources {
		if _, err := tx.Exec(ctx,
			`INSERT INTO task_resources(task_id,resource_id,status,requested_at)
			 VALUES($1,$2,'wanted',$3)`, req.ID, rid, now); err != nil {
			return nil, err
		}
		if _, err := tx.Exec(ctx,
			`INSERT INTO hold_ledger(task_id,resource_id,action,fence_epoch)
			 VALUES($1,$2,'wanted',0)`, req.ID, rid); err != nil {
			return nil, err
		}
	}

	res := &AcquireResult{}

	// Dynamic graph check: adding this waiter must not close a cycle.
	cycles, err := s.cycleFrom(ctx, tx, req.ID)
	if err != nil {
		return nil, err
	}
	if len(cycles) > 0 {
		detail, _ := json.Marshal(cycles)
		if _, err := tx.Exec(ctx,
			`UPDATE tasks SET state='failed', last_error=$2, updated_at=now()
			 WHERE id=$1`, req.ID, "deadlock cycle rejected: "+string(detail)); err != nil {
			return nil, err
		}
		// A rejected task holds nothing and must not block the graph.
		if _, err := tx.Exec(ctx,
			`DELETE FROM task_resources WHERE task_id=$1 AND status='wanted'`, req.ID); err != nil {
			return nil, err
		}
		s.logEvent(ctx, tx, req.ID, "rejected_cycle", string(detail))
		t, _ := s.loadTask(ctx, tx, req.ID, false)
		res.Status = "rejected"
		res.Task = s.withAging(t)
		res.Cycles = cycles
		if err := tx.Commit(ctx); err != nil {
			return nil, err
		}
		return res, nil
	}

	granted, err := s.tryGrant(ctx, tx, req.ID, true)
	if err != nil {
		return nil, err
	}
	t, err := s.loadTask(ctx, tx, req.ID, false)
	if err != nil {
		return nil, err
	}
	res.Task = s.withAging(t)
	if granted {
		res.Status = "granted"
		res.Holds, err = s.holdsFor(ctx, tx, req.ID)
		if err != nil {
			return nil, err
		}
		s.logEvent(ctx, tx, req.ID, "acquired",
			fmt.Sprintf("atomic grant of %d resources, lease until %s",
				len(req.Resources), t.LeaseExpires.Format(time.RFC3339Nano)))
	} else {
		res.Status = "waiting"
		res.WaitReasons, err = s.waitReasons(ctx, tx, req.ID)
		if err != nil {
			return nil, err
		}
		s.logEvent(ctx, tx, req.ID, "waiting",
			fmt.Sprintf("blocked on %v", req.Resources))
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return res, nil
}

// tryGrant grants ALL wanted rows of a task iff every one is free.
// Returns false (granting nothing) when a single resource is busy.
// freshTask=true means the task is newly created in 'waiting' state and
// should become 'running' with a fresh lease on success.
func (s *Service) tryGrant(ctx context.Context, tx pgx.Tx, taskID string, freshTask bool) (bool, error) {
	var blocked int
	if err := tx.QueryRow(ctx, `
		SELECT count(*)
		FROM task_resources w
		JOIN task_resources h
		  ON h.resource_id = w.resource_id AND h.status='held'
		 AND h.task_id <> w.task_id
		WHERE w.task_id=$1 AND w.status='wanted'`, taskID).Scan(&blocked); err != nil {
		return false, err
	}
	if blocked > 0 {
		return false, nil
	}

	var epoch int64
	if err := tx.QueryRow(ctx,
		`SELECT COALESCE((SELECT value::bigint FROM meta WHERE key='fence_epoch'),0)`).Scan(&epoch); err != nil {
		return false, err
	}
	now := s.now()

	var wanted []string
	rows, err := tx.Query(ctx,
		`SELECT resource_id FROM task_resources WHERE task_id=$1 AND status='wanted'
		 ORDER BY resource_id FOR UPDATE`, taskID)
	if err != nil {
		return false, err
	}
	for rows.Next() {
		var rid string
		if err := rows.Scan(&rid); err != nil {
			rows.Close()
			return false, err
		}
		wanted = append(wanted, rid)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return false, err
	}
	if len(wanted) == 0 {
		return false, nil
	}

	for _, rid := range wanted {
		if _, err := tx.Exec(ctx,
			`UPDATE task_resources SET status='held', granted_at=$2
			 WHERE task_id=$1 AND resource_id=$3`, taskID, now, rid); err != nil {
			return false, err
		}
		token, err := s.signEvidence(evidencePayload{
			Claim: "holder", TaskID: taskID, ResourceID: rid,
			GrantedAt: now, FenceEpoch: epoch, ServerID: s.serverID,
		})
		if err != nil {
			return false, err
		}
		if _, err := tx.Exec(ctx,
			`INSERT INTO hold_ledger(task_id,resource_id,action,fence_epoch,evidence)
			 VALUES($1,$2,'granted',$3,$4)`, taskID, rid, epoch, token); err != nil {
			return false, err
		}
	}

	if freshTask {
		var deadlineMs int
		if err := tx.QueryRow(ctx,
			`UPDATE tasks SET state='running', acquired_at=$2,
			        fence_epoch=$3,
			        lease_expires=$2::timestamptz + make_interval(secs => deadline_ms/1000.0),
			        updated_at=now()
			 WHERE id=$1 RETURNING deadline_ms`, taskID, now, epoch).Scan(&deadlineMs); err != nil {
			return false, err
		}
	} else {
		if _, err := tx.Exec(ctx,
			`UPDATE tasks SET updated_at=now() WHERE id=$1`, taskID); err != nil {
			return false, err
		}
	}
	return true, nil
}

// AddResources lets a running task dynamically request more resources.
// The new edges enter the wait-for graph immediately; if they would close
// a cycle the request is rejected (task keeps running with its old holds).
func (s *Service) AddResources(ctx context.Context, req AddResourcesReq) (*AcquireResult, error) {
	req.Resources = dedup(req.Resources)
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if err := lockAllocator(ctx, tx); err != nil {
		return nil, err
	}
	if err := s.resourcesExist(ctx, tx, req.Resources); err != nil {
		return nil, err
	}
	t, err := s.loadTask(ctx, tx, req.TaskID, true)
	if err != nil {
		return nil, err
	}
	if t.State != StateRunning {
		return nil, reject("only running tasks can request more resources (state=%s)", t.State)
	}

	now := s.now()
	var added []string
	for _, rid := range req.Resources {
		ct, err := tx.Exec(ctx,
			`INSERT INTO task_resources(task_id,resource_id,status,requested_at)
			 VALUES($1,$2,'wanted',$3)
			 ON CONFLICT (task_id,resource_id) DO NOTHING`,
			req.TaskID, rid, now)
		if err != nil {
			return nil, err
		}
		if ct.RowsAffected() > 0 {
			added = append(added, rid)
			_, _ = tx.Exec(ctx,
				`INSERT INTO hold_ledger(task_id,resource_id,action,fence_epoch)
				 VALUES($1,$2,'wanted',0)`, req.TaskID, rid)
		}
	}
	if len(added) == 0 {
		return nil, reject("all requested resources already requested or held")
	}

	res := &AcquireResult{}
	cycles, err := s.cycleFrom(ctx, tx, req.TaskID)
	if err != nil {
		return nil, err
	}
	if len(cycles) > 0 {
		detail, _ := json.Marshal(cycles)
		if _, err := tx.Exec(ctx,
			`DELETE FROM task_resources WHERE task_id=$1 AND status='wanted'
			 AND resource_id = ANY($2)`, req.TaskID, added); err != nil {
			return nil, err
		}
		s.logEvent(ctx, tx, req.TaskID, "dynamic_request_cycle_rejected", string(detail))
		t2, _ := s.loadTask(ctx, tx, req.TaskID, false)
		res.Status = "rejected"
		res.Task = s.withAging(t2)
		res.Cycles = cycles
		if err := tx.Commit(ctx); err != nil {
			return nil, err
		}
		return res, nil
	}

	granted, err := s.tryGrant(ctx, tx, req.TaskID, false)
	if err != nil {
		return nil, err
	}
	t2, err := s.loadTask(ctx, tx, req.TaskID, false)
	if err != nil {
		return nil, err
	}
	res.Task = s.withAging(t2)
	if granted {
		res.Status = "granted"
		res.Holds, _ = s.holdsFor(ctx, tx, req.TaskID)
		s.logEvent(ctx, tx, req.TaskID, "dynamic_grant",
			fmt.Sprintf("dynamically acquired %v", added))
	} else {
		res.Status = "waiting"
		res.WaitReasons, _ = s.waitReasons(ctx, tx, req.TaskID)
		s.logEvent(ctx, tx, req.TaskID, "dynamic_wait",
			fmt.Sprintf("holding existing resources, blocked requesting %v", added))
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return res, nil
}

// ------------------------------------------------------------ scheduling

// promote scans waiting tasks in effective-priority order (priority with
// aging bonus) and grants every task whose full wanted set is now free.
// Uncertain holders still block: fenced resources are never given away.
func (s *Service) promote(ctx context.Context, tx pgx.Tx) (int, error) {
	grantedTotal := 0
	for {
		rows, err := tx.Query(ctx, `
			SELECT w.task_id, min(w.requested_at) AS since
			FROM task_resources w
			WHERE w.status='wanted'
			GROUP BY w.task_id`)
		if err != nil {
			return grantedTotal, err
		}
		type cand struct {
			id    string
			since time.Time
		}
		var cands []cand
		for rows.Next() {
			var c cand
			if err := rows.Scan(&c.id, &c.since); err != nil {
				rows.Close()
				return grantedTotal, err
			}
			cands = append(cands, c)
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return grantedTotal, err
		}
		if len(cands) == 0 {
			return grantedTotal, nil
		}

		now := s.now()
		// Priority aging: wait time lowers the effective priority number,
		// bounded by AgingCap so a low-priority task can age to the front
		// but never beyond the configured urgency floor.
		type scored struct {
			c     cand
			score int
		}
		var ss []scored
		for _, c := range cands {
			var prio int
			if err := tx.QueryRow(ctx,
				`SELECT priority FROM tasks WHERE id=$1`, c.id).Scan(&prio); err != nil {
				return grantedTotal, err
			}
			bonus := int(now.Sub(c.since) / s.Cfg.AgingStep)
			if bonus > s.Cfg.AgingCap {
				bonus = s.Cfg.AgingCap
			}
			ss = append(ss, scored{c, prio - bonus*s.Cfg.AgingBonusPerStep})
		}
		// deterministic ordering: effective score, then wait time, then id
		for i := 0; i < len(ss); i++ {
			for j := i + 1; j < len(ss); j++ {
				if ss[j].score < ss[i].score ||
					(ss[j].score == ss[i].score && ss[j].c.since.Before(ss[i].c.since)) ||
					(ss[j].score == ss[i].score && ss[j].c.since.Equal(ss[i].c.since) && ss[j].c.id < ss[i].c.id) {
					ss[i], ss[j] = ss[j], ss[i]
				}
			}
		}

		madeProgress := false
		for _, sc := range ss {
			var state string
			if err := tx.QueryRow(ctx,
				`SELECT state FROM tasks WHERE id=$1`, sc.c.id).Scan(&state); err != nil {
				return grantedTotal, err
			}
			if state != StateWaiting && state != StateRunning {
				continue
			}
			ok, err := s.tryGrant(ctx, tx, sc.c.id, state == StateWaiting)
			if err != nil {
				return grantedTotal, err
			}
			if ok {
				madeProgress = true
				grantedTotal++
				s.logEvent(ctx, tx, sc.c.id, "promoted",
					fmt.Sprintf("granted by scheduler with effective priority %d", sc.score))
			}
		}
		if !madeProgress {
			return grantedTotal, nil
		}
	}
}

// releaseAll frees every held resource of a task and appends release
// evidence. It does not touch the task row itself.
func (s *Service) releaseAll(ctx context.Context, tx pgx.Tx, taskID string, reason string) error {
	rows, err := tx.Query(ctx,
		`SELECT resource_id FROM task_resources WHERE task_id=$1 AND status='held'`, taskID)
	if err != nil {
		return err
	}
	var rids []string
	for rows.Next() {
		var rid string
		if err := rows.Scan(&rid); err != nil {
			rows.Close()
			return err
		}
		rids = append(rids, rid)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}
	var epoch int64
	if err := tx.QueryRow(ctx,
		`SELECT COALESCE((SELECT value::bigint FROM meta WHERE key='fence_epoch'),0)`).Scan(&epoch); err != nil {
		return err
	}
	for _, rid := range rids {
		if _, err := tx.Exec(ctx,
			`INSERT INTO hold_ledger(task_id,resource_id,action,fence_epoch,evidence)
			 VALUES($1,$2,'released',$3,$4)`, taskID, rid, epoch, reason); err != nil {
			return err
		}
	}
	if _, err := tx.Exec(ctx,
		`DELETE FROM task_resources WHERE task_id=$1`, taskID); err != nil {
		return err
	}
	return nil
}

// ------------------------------------------------------- lifecycle ops

// Complete is the worker's late or timely finish report. Stale fence epochs
// (an old worker talking after a restart) are rejected.
func (s *Service) Complete(ctx context.Context, taskID string, fenceEpoch int64) (*TaskDetail, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if err := lockAllocator(ctx, tx); err != nil {
		return nil, err
	}
	t, err := s.loadTask(ctx, tx, taskID, true)
	if err != nil {
		return nil, err
	}
	if fenceEpoch != t.FenceEpoch || t.FenceEpoch == 0 {
		return nil, reject("stale or missing fence epoch %d: task is on epoch %d (old worker rejected)",
			fenceEpoch, t.FenceEpoch)
	}
	switch t.State {
	case StateCompleted:
		// idempotent
	case StateRunning, StateUncertain:
	case StateRevoking:
		// A revocation is pending: the worker's own word that it finished
		// is not enough. Resources stay held until an operator confirms
		// the physical stop, otherwise a runaway robot could keep moving.
		tx.Rollback(ctx)
		return nil, reject("task is being revoked; completion is not accepted until stop is confirmed")
	default:
		tx.Rollback(ctx)
		return nil, reject("cannot complete task in state %s", t.State)
	}
	if t.State != StateCompleted {
		if _, err := tx.Exec(ctx,
			`UPDATE tasks SET state='completed', updated_at=now() WHERE id=$1`,
			taskID); err != nil {
			return nil, err
		}
	}
	if err := s.releaseAll(ctx, tx, taskID, "task completed"); err != nil {
		return nil, err
	}
	s.logEvent(ctx, tx, taskID, "completed",
		fmt.Sprintf("late=%v", t.State == StateUncertain))
	if _, err := s.promote(ctx, tx); err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return s.TaskDetailFor(ctx, taskID)
}

// Heartbeat extends the running lease; rejected once uncertain.
func (s *Service) Heartbeat(ctx context.Context, taskID string, fenceEpoch int64) (*Task, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	t, err := s.loadTask(ctx, tx, taskID, true)
	if err != nil {
		tx.Rollback(ctx)
		return nil, err
	}
	if fenceEpoch != t.FenceEpoch || t.FenceEpoch == 0 {
		tx.Rollback(ctx)
		return nil, reject("stale or missing fence epoch %d: task is on epoch %d", fenceEpoch, t.FenceEpoch)
	}
	if t.State != StateRunning {
		tx.Rollback(ctx)
		return nil, reject("heartbeat only valid while running (state=%s)", t.State)
	}
	if _, err := tx.Exec(ctx,
		`UPDATE tasks SET lease_expires=now() + make_interval(secs => deadline_ms/1000.0),
		        updated_at=now()
		 WHERE id=$1`, taskID); err != nil {
		tx.Rollback(ctx)
		return nil, err
	}
	s.logEvent(ctx, tx, taskID, "heartbeat", "lease extended")
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	t2, err := scanTask(s.pool.QueryRow(ctx, `SELECT `+taskCols+` FROM tasks WHERE id=$1`, taskID))
	if err != nil {
		return nil, err
	}
	tt := s.withAging(t2)
	return &tt, nil
}

// SweepTimeouts moves running tasks whose lease has expired into 'uncertain'.
// Resources are NOT released: until the worker is confirmed stopped it might
// still finish late, so handing the resources to another task would be unsafe.
func (s *Service) SweepTimeouts(ctx context.Context) ([]string, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if err := lockAllocator(ctx, tx); err != nil {
		return nil, err
	}
	rows, err := tx.Query(ctx,
		`UPDATE tasks SET state='uncertain', lease_expires=NULL, updated_at=now()
		 WHERE state='running' AND lease_expires IS NOT NULL AND lease_expires < now()
		 RETURNING id`)
	if err != nil {
		return nil, err
	}
	var timedOut []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return nil, err
		}
		timedOut = append(timedOut, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	for _, id := range timedOut {
		s.logEvent(ctx, tx, id, "timeout_uncertain",
			"lease expired; holds fenced, awaiting late completion or confirmed stop")
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return timedOut, nil
}

// Revoke requests a stop. Held resources are only released later, when stop
// is confirmed. A waiting task holds nothing, so it is revoked immediately.
func (s *Service) Revoke(ctx context.Context, taskID string) (*TaskDetail, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if err := lockAllocator(ctx, tx); err != nil {
		return nil, err
	}
	t, err := s.loadTask(ctx, tx, taskID, true)
	if err != nil {
		return nil, err
	}
	switch t.State {
	case StateRunning, StateUncertain:
		if _, err := tx.Exec(ctx,
			`UPDATE tasks SET state='revoking', revoke_requested_at=now(), updated_at=now()
			 WHERE id=$1`, taskID); err != nil {
			return nil, err
		}
		s.logEvent(ctx, tx, taskID, "revoke_requested",
			"stop requested; resources remain held until confirmed stop")
	case StateWaiting:
		if _, err := tx.Exec(ctx,
			`UPDATE tasks SET state='revoked', updated_at=now() WHERE id=$1`, taskID); err != nil {
			return nil, err
		}
		if _, err := tx.Exec(ctx,
			`DELETE FROM task_resources WHERE task_id=$1 AND status='wanted'`, taskID); err != nil {
			return nil, err
		}
		s.logEvent(ctx, tx, taskID, "revoked", "waiting task cancelled without ever holding")
		if _, err := s.promote(ctx, tx); err != nil {
			return nil, err
		}
	default:
		return nil, reject("cannot revoke task in state %s", t.State)
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return s.TaskDetailFor(ctx, taskID)
}

// ConfirmStop is the operator/robot assertion that the worker has really
// stopped. Only now are held resources released and waiters promoted.
func (s *Service) ConfirmStop(ctx context.Context, taskID string) (*TaskDetail, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if err := lockAllocator(ctx, tx); err != nil {
		return nil, err
	}
	t, err := s.loadTask(ctx, tx, taskID, true)
	if err != nil {
		return nil, err
	}
	if t.State != StateRevoking && t.State != StateUncertain {
		return nil, reject("confirm-stop requires revoking or uncertain state (state=%s)", t.State)
	}
	if _, err := tx.Exec(ctx,
		`UPDATE tasks SET state='revoked', updated_at=now() WHERE id=$1`, taskID); err != nil {
		return nil, err
	}
	if err := s.releaseAll(ctx, tx, taskID, "stop confirmed by operator"); err != nil {
		return nil, err
	}
	s.logEvent(ctx, tx, taskID, "stop_confirmed",
		"worker verified stopped; held resources released")
	if _, err := s.promote(ctx, tx); err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return s.TaskDetailFor(ctx, taskID)
}

// ------------------------------------------------------------- queries

// TaskDetailFor returns full task state, holds with evidence and wait reasons.
func (s *Service) TaskDetailFor(ctx context.Context, taskID string) (*TaskDetail, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Commit(ctx)
	t, err := s.loadTask(ctx, tx, taskID, false)
	if err != nil {
		return nil, err
	}
	d := &TaskDetail{Task: s.withAging(t)}
	d.Holds, err = s.holdsFor(ctx, tx, taskID)
	if err != nil {
		return nil, err
	}
	d.WaitReasons, err = s.waitReasons(ctx, tx, taskID)
	if err != nil {
		return nil, err
	}
	return d, nil
}

func (s *Service) ListTasks(ctx context.Context) ([]Task, error) {
	rows, err := s.pool.Query(ctx, `SELECT `+taskCols+` FROM tasks ORDER BY created_at, id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Task
	for rows.Next() {
		t, err := scanTask(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, s.withAging(t))
	}
	return out, rows.Err()
}

// HeldCount returns how many resources a task currently holds. Used by
// tests and diagnostics to prove a waiting task holds nothing.
func (s *Service) HeldCount(ctx context.Context, taskID string) (int, error) {
	var n int
	if err := s.pool.QueryRow(ctx,
		`SELECT count(*) FROM task_resources WHERE task_id=$1 AND status='held'`,
		taskID).Scan(&n); err != nil {
		return 0, err
	}
	return n, nil
}

// WaitReasons exposes why a task is blocked (holder identity + state).
func (s *Service) WaitReasons(ctx context.Context, taskID string) ([]WaitReason, error) {
	if _, err := s.TaskDetailFor(ctx, taskID); err != nil {
		return nil, err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Commit(ctx)
	return s.waitReasons(ctx, tx, taskID)
}

// Graph renders the live wait-for graph and every directed cycle in it.
func (s *Service) Graph(ctx context.Context) (*GraphView, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Commit(ctx)

	g := &GraphView{}
	nrows, err := tx.Query(ctx, `
		SELECT t.id, t.state, t.priority,
		       COALESCE((SELECT array_agg(resource_id ORDER BY resource_id)
		                 FROM task_resources WHERE task_id=t.id AND status='held'), '{}'),
		       COALESCE((SELECT array_agg(resource_id ORDER BY resource_id)
		                 FROM task_resources WHERE task_id=t.id AND status='wanted'), '{}')
		FROM tasks t
		WHERE t.state IN ('waiting','running','uncertain','revoking')
		ORDER BY t.id`)
	if err != nil {
		return nil, err
	}
	for nrows.Next() {
		var n GraphNode
		if err := nrows.Scan(&n.TaskID, &n.State, &n.Priority, &n.Holds, &n.Wants); err != nil {
			nrows.Close()
			return nil, err
		}
		g.Nodes = append(g.Nodes, n)
	}
	nrows.Close()
	if err := nrows.Err(); err != nil {
		return nil, err
	}

	erows, err := tx.Query(ctx, `
		SELECT w.task_id, h.task_id, w.resource_id, r.kind
		FROM task_resources w
		JOIN task_resources h
		  ON h.resource_id=w.resource_id AND h.status='held' AND h.task_id<>w.task_id
		JOIN resources r ON r.id=w.resource_id
		WHERE w.status='wanted'
		ORDER BY w.task_id, w.resource_id`)
	if err != nil {
		return nil, err
	}
	for erows.Next() {
		var e GraphEdge
		if err := erows.Scan(&e.WaiterTask, &e.HolderTask, &e.ResourceID, &e.ResourceKind); err != nil {
			erows.Close()
			return nil, err
		}
		g.Edges = append(g.Edges, e)
	}
	erows.Close()
	if err := erows.Err(); err != nil {
		return nil, err
	}

	// One cycle search rooted at every waiter; dedupe by closed task path.
	seen := map[string]bool{}
	for _, n := range g.Nodes {
		// only nodes that wait can close a cycle involving them
		var wants int
		if err := tx.QueryRow(ctx,
			`SELECT count(*) FROM task_resources WHERE task_id=$1 AND status='wanted'`,
			n.TaskID).Scan(&wants); err != nil {
			return nil, err
		}
		if wants == 0 {
			continue
		}
		cycles, err := s.cycleFrom(ctx, tx, n.TaskID)
		if err != nil {
			return nil, err
		}
		for _, c := range cycles {
			key := strings.Join(c.Tasks, ">")
			if !seen[key] {
				seen[key] = true
				g.Cycles = append(g.Cycles, c)
			}
		}
	}
	return g, nil
}

// Events returns the audit trail of a task.
func (s *Service) Events(ctx context.Context, taskID string, limit int) ([]map[string]any, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	rows, err := s.pool.Query(ctx, `
		SELECT id, event, detail, occurred_at
		FROM task_events WHERE task_id=$1
		ORDER BY id DESC LIMIT $2`, taskID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []map[string]any
	for rows.Next() {
		var id int64
		var event, detail string
		var at time.Time
		if err := rows.Scan(&id, &event, &detail, &at); err != nil {
			return nil, err
		}
		out = append(out, map[string]any{
			"id": id, "event": event, "detail": detail, "occurredAt": at,
		})
	}
	return out, rows.Err()
}

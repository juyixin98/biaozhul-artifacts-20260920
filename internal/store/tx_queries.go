package store

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

func jsonMarshal(v any) ([]byte, error) { return json.Marshal(v) }

// GetExecutionTx loads an execution header inside a transaction.
func GetExecutionTx(ctx context.Context, tx pgx.Tx, id uuid.UUID) (ExecutionRow, error) {
	var e ExecutionRow
	err := tx.QueryRow(ctx,
		`SELECT id,tree_name,tree_version,content_hash,status,next_tick,created_at,finalized_at
		 FROM executions WHERE id=$1`, id).
		Scan(&e.ID, &e.TreeName, &e.TreeVersion, &e.ContentHash, &e.Status,
			&e.NextTick, &e.CreatedAt, &e.FinalizedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return ExecutionRow{}, ErrNotFound
	}
	return e, err
}

// LockedExecution is the execution header read under FOR UPDATE so ticks on
// the same execution are serialized even across processes.
type LockedExecution struct {
	Status   string
	NextTick int64
}

// LockExecutionForTick takes a row lock on the execution and returns its
// current state. ErrNotFound for unknown ids; ErrConflict if the execution is
// already terminal (success/failure/canceled).
func LockExecutionForTick(ctx context.Context, tx pgx.Tx, id uuid.UUID) (LockedExecution, int64, error) {
	var le LockedExecution
	err := tx.QueryRow(ctx,
		`SELECT status,next_tick FROM executions WHERE id=$1 FOR UPDATE`, id).
		Scan(&le.Status, &le.NextTick)
	if errors.Is(err, pgx.ErrNoRows) {
		return LockedExecution{}, 0, ErrNotFound
	}
	if err != nil {
		return LockedExecution{}, 0, err
	}
	if le.Status != StatusRunning {
		return le, le.NextTick, ErrConflict
	}
	return le, le.NextTick, nil
}

// InsertTick records the start of a tick attempt.
func InsertTick(ctx context.Context, tx pgx.Tx, execID uuid.UUID, seq int64) error {
	_, err := tx.Exec(ctx,
		`INSERT INTO ticks(execution_id,seq,status) VALUES ($1,$2,'running')`,
		execID, seq)
	return err
}

// InsertInterruptedTick records a tick attempt whose context was canceled
// before commit. It uses a separate connection because the tick transaction
// itself rolled back.
func (s *Store) InsertInterruptedTick(ctx context.Context, execID uuid.UUID, seq int64, note string) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO ticks(execution_id,seq,status,note,interrupted_at)
		 VALUES ($1,$2,'interrupted',$3,now())`, execID, seq, note)
	return err
}

// CommitTick finalizes a tick row and advances the execution header.
func CommitTick(ctx context.Context, tx pgx.Tx, execID uuid.UUID, seq int64, status, note string, terminal bool) error {
	if _, err := tx.Exec(ctx,
		`UPDATE ticks SET status=$3,note=$4,committed_at=now()
		 WHERE execution_id=$1 AND seq=$2 AND status='running'`,
		execID, seq, status, note); err != nil {
		return err
	}
	if terminal {
		_, err := tx.Exec(ctx,
			`UPDATE executions
			   SET next_tick=$2,status=$3,last_tick_at=now(),finalized_at=now()
			 WHERE id=$1`, execID, seq+1, status)
		return err
	}
	_, err := tx.Exec(ctx,
		`UPDATE executions SET next_tick=$2,last_tick_at=now() WHERE id=$1`,
		execID, seq+1)
	return err
}

// UpsertNodeState writes node status and detail for one tick.
func UpsertNodeState(ctx context.Context, tx pgx.Tx, execID uuid.UUID, seq int64, r NodeStateRow) error {
	_, err := tx.Exec(ctx,
		`INSERT INTO node_states(execution_id,node_id,status,detail,updated_tick)
		 VALUES ($1,$2,$3,$4,$5)
		 ON CONFLICT (execution_id,node_id)
		 DO UPDATE SET status=$3,detail=$4,updated_tick=$5`,
		execID, r.NodeID, r.Status, string(r.Detail), seq)
	return err
}

// LoadNodeStates reads the current node-state snapshot inside a tx.
func LoadNodeStates(ctx context.Context, tx pgx.Tx, execID uuid.UUID) (map[string]NodeStateRow, error) {
	rows, err := tx.Query(ctx,
		`SELECT node_id,status,detail,updated_tick
		 FROM node_states WHERE execution_id=$1`, execID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	list, err := collectNodeStates(rows)
	if err != nil {
		return nil, err
	}
	out := make(map[string]NodeStateRow, len(list))
	for _, r := range list {
		out[r.NodeID] = r
	}
	return out, nil
}

// InvocationInsert is data for creating a fresh invocation.
type InvocationInsert struct {
	NodeID      string
	StableKey   string
	Action      string
	Idempotent  bool
	Params      []byte
	CreatedTick int64
}

// InsertInvocation creates a 'pending' invocation row.
func InsertInvocation(ctx context.Context, tx pgx.Tx, execID uuid.UUID, in InvocationInsert) error {
	_, err := tx.Exec(ctx,
		`INSERT INTO invocations(execution_id,node_id,stable_key,action,idempotent,
		   params,status,attempt,dispatches,created_tick,updated_tick)
		 VALUES ($1,$2,$3,$4,$5,$6,'pending',0,0,$7,$7)`,
		execID, in.NodeID, in.StableKey, in.Action, in.Idempotent,
		string(in.Params), in.CreatedTick)
	return err
}

// ClaimInvocation marks a pending (or still-running on restart) invocation as
// dispatched under a new attempt and returns its data plus the new attempt
// number. It is the cross-process dedup gate: only the claimer may run the
// stub body. Returns ErrNotFound when the row is already terminal/canceled or
// missing.
func (s *Store) ClaimInvocation(ctx context.Context, execID uuid.UUID, stableKey string) (InvocationRow, int64, error) {
	var r InvocationRow
	var params []byte
	var result []byte
	err := s.Tx(ctx, func(tx pgx.Tx) error {
		var status string
		err := tx.QueryRow(ctx,
			`SELECT status FROM invocations
			  WHERE execution_id=$1 AND stable_key=$2 FOR UPDATE`,
			execID, stableKey).Scan(&status)
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		if status == "success" || status == "failure" || status == "canceled" {
			return ErrConflict
		}
		if _, err := tx.Exec(ctx,
			`UPDATE invocations
			   SET attempt=attempt+1, dispatches=dispatches+1,
			       status='running', updated_at=now()
			 WHERE execution_id=$1 AND stable_key=$2`, execID, stableKey); err != nil {
			return err
		}
		return tx.QueryRow(ctx,
			`SELECT node_id,stable_key,action,idempotent,params,status,attempt,
			        COALESCE(result,'null'::jsonb),COALESCE(error,''),dispatches,
			        created_tick,updated_tick
			 FROM invocations WHERE execution_id=$1 AND stable_key=$2`,
			execID, stableKey).Scan(
			&r.NodeID, &r.StableKey, &r.Action, &r.Idempotent, &params,
			&r.Status, &r.Attempt, &result, &r.Err, &r.Dispatches,
			&r.CreatedTick, &r.UpdatedTick)
	})
	r.Params = params
	r.Result = result
	if errors.Is(err, ErrConflict) {
		return r, 0, ErrConflict
	}
	return r, r.Attempt, err
}

// CompleteInvocation applies a worker result. The optimistic WHERE clause is
// the late-result barrier: the report lands only if it carries the current
// attempt and the invocation is still 'running'. A stale attempt, or a
// cancellation that won the race, affects zero rows and the result is
// discarded — the tree can never be resurrected by a late report.
// Returns true when the report was accepted.
func (s *Store) CompleteInvocation(ctx context.Context, execID uuid.UUID, stableKey string, attempt int64, success bool, resultJSON []byte, errMsg string) (bool, error) {
	status := "success"
	if !success {
		status = "failure"
	}
	if len(resultJSON) == 0 {
		resultJSON = []byte("null")
	}
	tag, err := s.pool.Exec(ctx,
		`UPDATE invocations
		   SET status=$3,result=$4,error=$5,updated_tick=updated_tick,updated_at=now()
		 WHERE execution_id=$1 AND stable_key=$2
		   AND status='running' AND attempt=$6`,
		execID, stableKey, status, string(resultJSON), errMsg, attempt)
	if err != nil {
		return false, err
	}
	return tag.RowsAffected() == 1, nil
}

// CancelInvocationsByStableKeys cancels a set of active invocations within a
// tick tx (parent subtree cancellation). Returns the stable keys that were
// actually active so the runner can cancel their in-process workers.
func CancelInvocationsByStableKeys(ctx context.Context, tx pgx.Tx, execID uuid.UUID, keys []string) ([]string, error) {
	if len(keys) == 0 {
		return nil, nil
	}
	rows, err := tx.Query(ctx,
		`UPDATE invocations SET status='canceled',updated_at=now()
		 WHERE execution_id=$1 AND stable_key=ANY($2) AND status IN ('pending','running')
		 RETURNING stable_key`, execID, keys)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var k string
		if err := rows.Scan(&k); err != nil {
			return nil, err
		}
		out = append(out, k)
	}
	return out, rows.Err()
}

// LoadInvocation reads one invocation inside a transaction.
func LoadInvocation(ctx context.Context, tx pgx.Tx, execID uuid.UUID, stableKey string) (InvocationRow, error) {
	var r InvocationRow
	err := tx.QueryRow(ctx,
		`SELECT node_id,stable_key,action,idempotent,params,status,attempt,
		        COALESCE(result,'null'::jsonb),COALESCE(error,''),dispatches,
		        created_tick,updated_tick
		 FROM invocations WHERE execution_id=$1 AND stable_key=$2`,
		execID, stableKey).Scan(
		&r.NodeID, &r.StableKey, &r.Action, &r.Idempotent, &r.Params,
		&r.Status, &r.Attempt, &r.Result, &r.Err, &r.Dispatches,
		&r.CreatedTick, &r.UpdatedTick)
	if errors.Is(err, pgx.ErrNoRows) {
		return InvocationRow{}, ErrNotFound
	}
	return r, err
}

// MarkExecutionCanceled cancels an execution outright (DELETE /cancel).
// Returns ErrNotFound if unknown, ErrConflict if already terminal.
func (s *Store) MarkExecutionCanceled(ctx context.Context, id uuid.UUID) error {
	return s.Tx(ctx, func(tx pgx.Tx) error {
		var status string
		err := tx.QueryRow(ctx, `SELECT status FROM executions WHERE id=$1 FOR UPDATE`, id).Scan(&status)
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		if status != StatusRunning {
			return ErrConflict
		}
		// Read next_tick for the tick record.
		var seq int64
		if err := tx.QueryRow(ctx,
			`SELECT next_tick FROM executions WHERE id=$1`, id).Scan(&seq); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx,
			`INSERT INTO ticks(execution_id,seq,status,note,committed_at)
			 VALUES ($1,$2,'canceled','execution canceled by client',now())`, id, seq); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx,
			`UPDATE invocations SET status='canceled',updated_at=now()
			 WHERE execution_id=$1 AND status IN ('pending','running')`, id); err != nil {
			return err
		}
		_, err = tx.Exec(ctx,
			`UPDATE executions
			   SET status='canceled',next_tick=$2,last_tick_at=now(),finalized_at=now()
			 WHERE id=$1 AND status='running'`, id, seq+1)
		return err
	})
}

// OrphanedActiveInvocations lists every pending/running invocation across
// running executions (startup recovery).
func (s *Store) OrphanedActiveInvocations(ctx context.Context) ([]struct {
	ExecID uuid.UUID
	Row    InvocationRow
}, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT execution_id,node_id,stable_key,action,idempotent,params,status,attempt,
		        COALESCE(result,'null'::jsonb),COALESCE(error,''),dispatches,
		        created_tick,updated_tick
		 FROM invocations WHERE status IN ('pending','running')`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []struct {
		ExecID uuid.UUID
		Row    InvocationRow
	}
	for rows.Next() {
		var v struct {
			ExecID uuid.UUID
			Row    InvocationRow
		}
		if err := rows.Scan(&v.ExecID, &v.Row.NodeID, &v.Row.StableKey, &v.Row.Action,
			&v.Row.Idempotent, &v.Row.Params, &v.Row.Status, &v.Row.Attempt,
			&v.Row.Result, &v.Row.Err, &v.Row.Dispatches,
			&v.Row.CreatedTick, &v.Row.UpdatedTick); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

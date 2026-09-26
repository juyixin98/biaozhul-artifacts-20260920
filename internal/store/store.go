// Package store 实现幂等记录与业务副作用的本地持久化。
//
// 持久化介质是一个 fsync 过的预写日志（WAL），每条 commit 记录原子地包含
// 【HTTP 执行结果】与【业务副作用（账本流水 + 余额变更）】。读取路径完全在
// 内存快照上进行；commit 必须先追加日志（含 fsync）才更新内存，从而保证：
// 只要调用方观察到成功，副作用就已经落盘；崩溃恢复后二者要么都在、要么都不在。
package store

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
	"time"
)

const walFileName = "wal.log"

const (
	opPending = "pending"
	opCommit  = "commit"
	opRelease = "release"

	// Pending 处理中；Completed 已提交。
	Pending   = "pending"
	Completed = "completed"
)

// 哨兵错误，由 HTTP 层映射为对应的状态码与结构化错误体。
var (
	// ErrConflict 同键已有记录，但请求摘要不同（无论对方处理中还是已完成）。
	ErrConflict = errors.New("idempotency key reused with a different request payload")
	// ErrInProgress 同键同摘要的请求正在处理中。
	ErrInProgress = errors.New("a request with the same idempotency key is still being processed")
	// ErrLeaseLost 占位代次已变更（被 TTL 回收并由他人接管），本次执行结果必须丢弃。
	ErrLeaseLost = errors.New("idempotency lease lost to a newer attempt")
)

// Effect 是一次业务副作用：一笔账户流水。
type Effect struct {
	TxID     string    `json:"tx_id"`
	Account  string    `json:"account"`
	Amount   int64     `json:"amount"` // 最小货币单位，整数避免浮点
	Kind     string    `json:"kind"`
	At       time.Time `json:"at"`
	Attempts int       `json:"attempts"` // 假外部调用次数（可能 >1，见 README 说明）
}

// Record 是某个幂等键的当前状态。
type Record struct {
	Key         string
	Fingerprint string
	Digest      string
	Status      string // Pending | Completed
	StatusCode  int
	Response    []byte
	Effect      *Effect
	PendingAt   time.Time
	ExpiresAt   time.Time
	CompletedAt time.Time
	Gen         uint64
	// BalanceAfter 仅对已完成记录有意义：提交瞬间锁内读到的账户余额。
	BalanceAfter int64
}

func recordFromOp(op walOp, status string) Record {
	r := Record{
		Key:         op.Key,
		Fingerprint: op.Fingerprint,
		Digest:      op.Digest,
		Status:      status,
		StatusCode:  op.StatusCode,
		Response:    append([]byte(nil), op.Response...),
		Effect:      op.Effect,
		Gen:         op.Gen,
	}
	if t, err := time.Parse(time.RFC3339Nano, op.At); err == nil {
		if status == Pending {
			r.PendingAt = t
		} else {
			r.CompletedAt = t
		}
	}
	if t, err := time.Parse(time.RFC3339Nano, op.ExpiresAt); err == nil && status == Pending {
		r.ExpiresAt = t
	}
	return r
}

// memState 是内存中的可序列化状态形态。
type memState struct {
	Balances map[string]int64  `json:"balances"`
	Ledger   []Effect          `json:"ledger"`
	Keys     map[string]Record `json:"keys"`
}

// Store 是线程安全的幂等存储。
type Store struct {
	dir        string
	wal        *os.File
	mu         sync.Mutex
	state      memState
	maxGen     map[string]uint64
	pendingTTL time.Duration
	now        func() time.Time
}

// Options 控制存储行为。
type Options struct {
	Dir        string
	PendingTTL time.Duration
	Now        func() time.Time
}

// Open 打开（必要时创建）存储目录并重放 WAL。
func Open(opts Options) (*Store, error) {
	if opts.PendingTTL <= 0 {
		opts.PendingTTL = 30 * time.Second
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if err := os.MkdirAll(opts.Dir, 0o755); err != nil {
		return nil, fmt.Errorf("create data dir: %w", err)
	}
	walPath := filepath.Join(opts.Dir, walFileName)
	wal, err := os.OpenFile(walPath, os.O_RDWR|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return nil, fmt.Errorf("open wal: %w", err)
	}
	s := &Store{
		dir:        opts.Dir,
		wal:        wal,
		pendingTTL: opts.PendingTTL,
		now:        opts.Now,
		maxGen:     map[string]uint64{},
		state: memState{
			Balances: map[string]int64{},
			Ledger:   []Effect{},
			Keys:     map[string]Record{},
		},
	}
	if err := s.recover(); err != nil {
		_ = wal.Close()
		return nil, fmt.Errorf("recover wal: %w", err)
	}
	return s, nil
}

// Close 刷新并关闭日志文件。
func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.wal.Sync(); err != nil {
		return err
	}
	return s.wal.Close()
}

// Begin 尝试为 key 建立"处理中"占位。语义：
//   - 键不存在：原子写入 pending（fsync），返回新记录与 nil
//     —— 并发竞争者中只有一个能走到这里
//   - 已有同摘要的 completed 记录：返回该记录与 nil（重放，不执行副作用）
//   - 已有不同摘要的记录（pending 或 completed，无论是否过期）：ErrConflict
//   - 已有同摘要且未过期的 pending：ErrInProgress
//   - 已有同摘要但已过期的 pending：以【新代次】回收占位并返回新记录；
//     旧执行者即使迟到，其 Commit 也会因代次不匹配被拒（防止僵尸提交）
func (s *Store) Begin(key, fingerprint, digest string) (Record, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	now := s.now()
	if existing, ok := s.state.Keys[key]; ok {
		if existing.Fingerprint != fingerprint {
			return Record{}, ErrConflict
		}
		if existing.Status == Completed {
			return existing, nil
		}
		if existing.ExpiresAt.IsZero() || !now.After(existing.ExpiresAt) {
			return existing, ErrInProgress
		}
		// 同摘要、占位已过期：同代次回收（gen 递增）。
		gen := existing.Gen + 1
		if m := s.maxGen[key]; m >= gen {
			gen = m + 1
		}
		s.maxGen[key] = gen
		r := Record{
			Key:         key,
			Fingerprint: fingerprint,
			Digest:      digest,
			Status:      Pending,
			PendingAt:   now,
			ExpiresAt:   now.Add(s.pendingTTL),
			Gen:         gen,
		}
		if err := s.appendPending(r, now); err != nil {
			return Record{}, fmt.Errorf("persist reclaimed pending: %w", err)
		}
		s.state.Keys[key] = r
		return r, nil
	}

	gen := s.maxGen[key] + 1
	s.maxGen[key] = gen
	r := Record{
		Key:         key,
		Fingerprint: fingerprint,
		Digest:      digest,
		Status:      Pending,
		PendingAt:   now,
		ExpiresAt:   now.Add(s.pendingTTL),
		Gen:         gen,
	}
	if err := s.appendPending(r, now); err != nil {
		// 落盘失败：内存不写入，下一个请求可重新抢占（与崩溃恢复语义一致）
		return Record{}, fmt.Errorf("persist pending: %w", err)
	}
	s.state.Keys[key] = r
	return r, nil
}

func (s *Store) appendPending(r Record, now time.Time) error {
	return s.appendOp(walOp{
		Op:          opPending,
		Key:         r.Key,
		Fingerprint: r.Fingerprint,
		Digest:      r.Digest,
		At:          now.UTC().Format(time.RFC3339Nano),
		ExpiresAt:   r.ExpiresAt.UTC().Format(time.RFC3339Nano),
		Gen:         r.Gen,
	})
}

// Commit 在同一个本地事务（一条 WAL 记录 + 一次 fsync）中提交
// 【HTTP 执行结果】与【业务副作用】。原子性边界就是这条记录：恢复时二者
// 同时出现，不存在"副作用已入账但结果丢失"或反之的中间状态。
//
// effect 为 nil 时表示该结果没有业务副作用（例如入参校验失败，同样需要
// 被重放，避免同键重试得到不一致的结论）。
//
// buildResponse 在持有存储锁、且已算出提交后新余额时被回调，因此响应体里
// 若包含余额等"提交后状态"，其取值与副作用严格一致，且一起落进同一条记录。
//
// gen 是 Begin 授予的代次，构成 Compare-And-Set：占位被 TTL 回收并被他人
// 以新代次接管后，旧执行者的迟到提交在此被拒绝——本地副作用绝不重复入账。
func (s *Store) Commit(
	gen uint64, key string, statusCode int, effect *Effect,
	buildResponse func(balanceAfter int64) ([]byte, error),
) (Record, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	r, ok := s.state.Keys[key]
	if !ok {
		return Record{}, fmt.Errorf("commit %q: pending record not found", key)
	}
	if r.Status != Pending {
		return Record{}, fmt.Errorf("commit %q: record already completed", key)
	}
	if r.Gen != gen {
		return Record{}, fmt.Errorf("%w: commit %q: lease gen %d, stale attempt gen %d", ErrLeaseLost, key, r.Gen, gen)
	}

	// 先在锁内算出提交后的新余额，再用它生成响应体：二者必须来自同一状态点。
	newBalance := int64(0)
	if effect != nil {
		newBalance = s.state.Balances[effect.Account] + effect.Amount
	}
	response, err := buildResponse(newBalance)
	if err != nil {
		return Record{}, fmt.Errorf("build committed response: %w", err)
	}

	now := s.now().UTC()
	op := walOp{
		Op:          opCommit,
		Key:         key,
		Fingerprint: r.Fingerprint,
		Digest:      r.Digest,
		StatusCode:  statusCode,
		Response:    append([]byte(nil), response...),
		At:          now.Format(time.RFC3339Nano),
		Gen:         r.Gen,
	}
	if effect != nil {
		eff := *effect
		op.Effect = &eff
	}
	if err := s.appendOp(op); err != nil {
		return Record{}, fmt.Errorf("persist commit: %w", err)
	}

	r.Status = Completed
	r.StatusCode = statusCode
	r.Response = op.Response
	r.CompletedAt = now
	if effect != nil {
		eff := *effect
		r.Effect = &eff
		s.state.Ledger = append(s.state.Ledger, eff)
		s.state.Balances[eff.Account] = newBalance
		r.BalanceAfter = newBalance
	}
	s.state.Keys[key] = r
	return r, nil
}

// Release 释放占位（业务执行失败时调用）。副作用从未提交，之后同键同摘要可安全重试。
// 仅当持有的仍是当前代次时才释放，避免把接管者的新占位误删。
func (s *Store) Release(gen uint64, key string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	r, ok := s.state.Keys[key]
	if !ok || r.Status != Pending || r.Gen != gen {
		return nil
	}
	if err := s.appendOp(walOp{Op: opRelease, Key: key, Gen: r.Gen}); err != nil {
		return fmt.Errorf("persist release: %w", err)
	}
	delete(s.state.Keys, key)
	return nil
}

// Lookup 返回键的当前记录（不存在时 ok=false）。
func (s *Store) Lookup(key string) (Record, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.state.Keys[key]
	if ok {
		r.Response = append([]byte(nil), r.Response...)
	}
	return r, ok
}

// Ledger 返回已提交副作用（流水）的副本。
func (s *Store) Ledger() []Effect {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Effect, len(s.state.Ledger))
	copy(out, s.state.Ledger)
	return out
}

// Balance 返回某账户当前余额。
func (s *Store) Balance(account string) int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.state.Balances[account]
}

// Stats 返回用于测试报告的内部计数。
type Stats struct {
	Keys     int              `json:"idempotency_keys"`
	Ledger   int              `json:"committed_effects"`
	Balances map[string]int64 `json:"balances"`
}

// Stats 返回存储统计快照。
func (s *Store) Stats() Stats {
	s.mu.Lock()
	defer s.mu.Unlock()
	b := make(map[string]int64, len(s.state.Balances))
	for k, v := range s.state.Balances {
		b[k] = v
	}
	return Stats{Keys: len(s.state.Keys), Ledger: len(s.state.Ledger), Balances: b}
}

// Reset 清空全部状态并截断 WAL（仅供测试/验收场景间复位使用）。
func (s *Store) Reset() error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if err := s.wal.Truncate(0); err != nil {
		return fmt.Errorf("truncate wal: %w", err)
	}
	if _, err := s.wal.Seek(0, io.SeekStart); err != nil {
		return fmt.Errorf("seek wal: %w", err)
	}
	if err := s.wal.Sync(); err != nil {
		return fmt.Errorf("sync truncated wal: %w", err)
	}
	s.state = memState{Balances: map[string]int64{}, Ledger: []Effect{}, Keys: map[string]Record{}}
	s.maxGen = map[string]uint64{}
	return nil
}

// WALPath 暴露 WAL 位置（调试用）。
func (s *Store) WALPath() string { return filepath.Join(s.dir, walFileName) }

var _ io.Closer = (*Store)(nil)

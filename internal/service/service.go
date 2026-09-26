// Package service 编排幂等存款业务：占位 → 调用外部审计 → 本地事务提交。
package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"idempotentsave/internal/auditclient"
	"idempotentsave/internal/clock"
	"idempotentsave/internal/fakeaudit"
	"idempotentsave/internal/idem"
	"idempotentsave/internal/store"
)

// DepositRequest 是存款请求的业务字段（幂等键与故障头在外层传入）。
type DepositRequest struct {
	Account string `json:"account"`
	Amount  int64  `json:"amount"`
	Memo    string `json:"memo,omitempty"`
}

// Validate 做入站校验，返回面向用户的错误信息。
func (d DepositRequest) Validate() error {
	if d.Account == "" {
		return errors.New("account is required")
	}
	if d.Amount <= 0 {
		return errors.New("amount must be a positive integer")
	}
	return nil
}

// DepositResponse 是成功提交后保存并重放的响应体。
type DepositResponse struct {
	Status       string `json:"status"`
	TxID         string `json:"tx_id"`
	Account      string `json:"account"`
	Amount       int64  `json:"amount"`
	BalanceAfter int64  `json:"balance_after"`
	AuditCalls   int    `json:"audit_calls_in_attempt"`
}

// Lease 是一次成功占位后持有的"执行租约"。
type Lease struct {
	Key         string
	Fingerprint string
	Digest      string
	Gen         uint64
	ExpiresAt   time.Time
	hold        *Hold
}

// Sentinel errors.
var (
	ErrConflict   = store.ErrConflict
	ErrInProgress = store.ErrInProgress
)

// Service 业务服务。
type Service struct {
	store *store.Store
	audit *auditclient.Client
	clk   clock.Clock

	mu    sync.Mutex
	holds map[string]*Hold // 按幂等键索引当前等待点
}

// New 创建业务服务。
func New(st *store.Store, audit *auditclient.Client, clk clock.Clock) *Service {
	return &Service{store: st, audit: audit, clk: clk, holds: map[string]*Hold{}}
}

// Begin 校验请求、计算摘要并抢占占位。返回：
//   - lease != nil, nil：本请求获得执行权
//   - record != nil, nil：该键已有完成记录，调用方应重放
//   - err != nil：ErrConflict（同键不同摘要）或 ErrInProgress（同摘要处理中）
func (s *Service) Begin(key string, d DepositRequest, rawBody []byte) (*Lease, *store.Record, error) {
	fp := idem.Fingerprint("POST", "/v1/deposits", rawBody)
	digest := idem.BodyDigest(rawBody)
	rec, err := s.store.Begin(key, fp, digest)
	if err == nil {
		if rec.Status == store.Completed {
			return nil, &rec, nil // 已完成：调用方重放
		}
		hold := s.getOrCreateHold(key)
		l := &Lease{
			Key: key, Fingerprint: fp, Digest: digest,
			Gen: rec.Gen, ExpiresAt: rec.ExpiresAt, hold: hold,
		}
		return l, nil, nil
	}
	if errors.Is(err, store.ErrInProgress) {
		return nil, nil, ErrInProgress
	}
	if errors.Is(err, store.ErrConflict) {
		return nil, nil, ErrConflict
	}
	return nil, nil, fmt.Errorf("begin idempotent operation: %w", err)
}

// WaitForCompletion 等待该键当前占位被提交（供处理中重复请求使用）。
//
// 实现上同时利用内存 Hold 通道（胜者已注册时即时唤醒）与存储轮询
// （胜者尚未注册 Hold、或进程重启后只剩持久化占位时仍能正确等待）。
// 返回 true 仅当记录已提交；超时/释放/不存在返回 false。
func (s *Service) WaitForCompletion(ctx context.Context, key string, maxWait time.Duration) bool {
	deadline := time.Now().Add(maxWait)
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()

	for {
		if rec, ok := s.store.Lookup(key); ok {
			if rec.Status == store.Completed {
				return true
			}
		}
		// 找到当前 Hold 则优先在它的通道上等待；否则走轮询节拍。
		s.mu.Lock()
		h := s.holds[key]
		s.mu.Unlock()

		var doneCh <-chan struct{}
		if h != nil {
			doneCh = h.doneChan()
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			rec, ok := s.store.Lookup(key)
			return ok && rec.Status == store.Completed
		}
		waitTimer := time.NewTimer(remaining)
		select {
		case <-doneCh:
			// 胜者刚结束：下一轮循环查存储确认是否 committed
		case <-ticker.C:
			// 周期性兜底（应对 Hold 尚未注册的窗口）
		case <-waitTimer.C:
			rec, ok := s.store.Lookup(key)
			return ok && rec.Status == store.Completed
		case <-ctx.Done():
			rec, ok := s.store.Lookup(key)
			return ok && rec.Status == store.Completed
		}
		waitTimer.Stop()
	}
}

// Lookup 查询键当前记录。
func (s *Service) Lookup(key string) (store.Record, bool) { return s.store.Lookup(key) }

// RunExternal 执行业务的外部副作用调用（假审计系统）。
// 该调用不在本地事务内，因此不保证恰好一次（见包注释与 README）。
// 返回本次执行实际发起的外部调用次数。
func (s *Service) RunExternal(ctx context.Context, l *Lease, d DepositRequest) (int, error) {
	e := fakeaudit.Event{
		TxID:    txIDFor(l.Key, l.Gen),
		Account: d.Account,
		Amount:  d.Amount,
		IdemKey: l.Key,
		Attempt: l.Gen,
		At:      s.clk.Now(),
	}
	if err := s.audit.Record(ctx, e); err != nil {
		return 1, err
	}
	return 1, nil
}

// Commit 把【执行结果】与【本地副作用（流水+余额）】作为同一事务提交。
// 响应体在存储层算出提交后余额后才生成，保证余额字段与副作用来自同一状态点。
func (s *Service) Commit(l *Lease, d DepositRequest, auditCalls int) (store.Record, DepositResponse, error) {
	txID := txIDFor(l.Key, l.Gen)
	eff := store.Effect{
		TxID:     txID,
		Account:  d.Account,
		Amount:   d.Amount,
		Kind:     "deposit",
		At:       s.clk.Now(),
		Attempts: auditCalls,
	}
	var resp DepositResponse
	rec, err := s.store.Commit(l.Gen, l.Key, 200, &eff, func(balanceAfter int64) ([]byte, error) {
		resp = DepositResponse{
			Status:       "completed",
			TxID:         txID,
			Account:      d.Account,
			Amount:       d.Amount,
			BalanceAfter: balanceAfter,
			AuditCalls:   auditCalls,
		}
		return jsonMarshal(resp)
	})
	if err != nil {
		return store.Record{}, DepositResponse{}, err
	}
	l.hold.doneCommitted()
	s.dropHold(l.Key)
	return rec, resp, nil
}

// CommitValidationFailure 提交"无副作用的失败结果"（入站校验错误），
// 让同键重试得到一致结论。
func (s *Service) CommitValidationFailure(l *Lease, clientMsg string) (store.Record, error) {
	body, _ := jsonMarshal(map[string]string{"status": "rejected", "error": clientMsg})
	rec, err := s.store.Commit(l.Gen, l.Key, 422, nil, func(_ int64) ([]byte, error) {
		return body, nil
	})
	if err != nil {
		return store.Record{}, err
	}
	l.hold.doneCommitted()
	s.dropHold(l.Key)
	return rec, nil
}

// FailAndRelease 在外部步骤失败时释放租约（从未提交副作用，可安全重试）。
func (s *Service) FailAndRelease(l *Lease) error {
	if err := s.store.Release(l.Gen, l.Key); err != nil {
		return err
	}
	l.hold.doneReleased()
	s.dropHold(l.Key)
	return nil
}

// CurrentHold 返回键的当前等待点是否存在（测试辅助）。
func (s *Service) CurrentHold(key string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, ok := s.holds[key]
	return ok
}

func (s *Service) getOrCreateHold(key string) *Hold {
	s.mu.Lock()
	defer s.mu.Unlock()
	h := s.holds[key]
	if h == nil {
		h = newHold()
		s.holds[key] = h
	}
	return h
}

func (s *Service) dropHold(key string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.holds, key)
}

// txIDFor 由幂等键与代次确定性派生交易号：不同重试代次可区分，
// 但只有真正提交的代次才进入账本。
func txIDFor(key string, gen uint64) string {
	h := sha256.Sum256([]byte(key))
	return fmt.Sprintf("tx_%s_g%d", hex.EncodeToString(h[:8]), gen)
}

func jsonMarshal(v any) ([]byte, error) { return json.Marshal(v) }

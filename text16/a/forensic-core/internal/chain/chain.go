// Package chain 实现案件级只追加证据链：每条事件携带案件内序号、
// 前一事件摘要与规范化内容摘要；并发追加通过行锁与唯一约束防止分叉。
//
// 注意：哈希链只能提供完整性检查（发现缺失/篡改/乱序），
// 不能代替可信时间戳或外部签名。
package chain

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"

	"forensiccore/internal/models"
)

// GenesisPrev 每个案件第一条事件的前一摘要。
const GenesisPrev = "GENESIS"

const maxAppendRetries = 5

// Store 证据链存储。
type Store struct {
	DB *gorm.DB
}

// NewStore 创建证据链存储。
func NewStore(db *gorm.DB) *Store { return &Store{DB: db} }

// CanonicalJSON 生成规范化 JSON（Go 对 map 键排序，输出确定）。
func CanonicalJSON(v map[string]any) (string, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return "", fmt.Errorf("canonical json: %w", err)
	}
	return string(b), nil
}

// ContentHash 计算事件规范化内容摘要。payload 必须是 CanonicalJSON 的输出。
func ContentHash(seq uint64, typ, actor, payload, prevHash string, createdAtUnix int64) string {
	m := map[string]any{
		"actor":           actor,
		"created_at_unix": createdAtUnix,
		"payload":         payload,
		"prev_hash":       prevHash,
		"seq":             seq,
		"type":            typ,
	}
	b, _ := json.Marshal(m) // map 键有序，输出确定
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// Append 追加一条事件（自带事务与并发重试）。
func (s *Store) Append(caseID uint, typ, actor string, payload map[string]any) (*models.ChainEvent, error) {
	payloadJSON, err := CanonicalJSON(payload)
	if err != nil {
		return nil, err
	}
	var ev *models.ChainEvent
	for attempt := 0; attempt < maxAppendRetries; attempt++ {
		ev, err = s.appendOnce(caseID, typ, actor, payloadJSON)
		if err == nil {
			return ev, nil
		}
		if !isDuplicateKey(err) {
			return nil, err
		}
		// 并发追加在同一序号上冲突：序号已被对方占用，重试取新序号。
	}
	return nil, fmt.Errorf("chain append failed after %d retries: %w", maxAppendRetries, err)
}

func (s *Store) appendOnce(caseID uint, typ, actor, payloadJSON string) (*models.ChainEvent, error) {
	var ev models.ChainEvent
	err := s.DB.Transaction(func(tx *gorm.DB) error {
		return s.AppendTx(tx, caseID, typ, actor, payloadJSON, &ev)
	})
	if err != nil {
		return nil, err
	}
	return &ev, nil
}

// AppendTx 在调用方事务内追加事件。通过 SELECT ... FOR UPDATE 锁定案件行，
// 序列化同一案件的并发追加；(case_id, seq) 唯一索引作为兜底防分叉。
// payloadJSON 必须是 CanonicalJSON 的输出；out 可为 nil。
func (s *Store) AppendTx(tx *gorm.DB, caseID uint, typ, actor, payloadJSON string, out *models.ChainEvent) error {
	var c models.Case
	if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).First(&c, caseID).Error; err != nil {
		return fmt.Errorf("lock case %d: %w", caseID, err)
	}
	var last models.ChainEvent
	prev := GenesisPrev
	var seq uint64 = 1
	err := tx.Where("case_id = ?", caseID).Order("seq DESC").First(&last).Error
	switch {
	case err == nil:
		prev = last.Hash
		seq = last.Seq + 1
	case !errors.Is(err, gorm.ErrRecordNotFound):
		return err
	}
	now := time.Now().UTC()
	ev := models.ChainEvent{
		CaseID:        caseID,
		Seq:           seq,
		Type:          typ,
		Actor:         actor,
		Payload:       payloadJSON,
		PrevHash:      prev,
		CreatedAtUnix: now.UnixNano(),
		CreatedAt:     now,
	}
	ev.Hash = ContentHash(seq, typ, actor, payloadJSON, prev, ev.CreatedAtUnix)
	if err := tx.Create(&ev).Error; err != nil {
		return err
	}
	if out != nil {
		*out = ev
	}
	return nil
}

// Issue 链校验发现的问题。
type Issue struct {
	Seq    uint64 `json:"seq"`
	Kind   string `json:"kind"` // missing / tampered / out_of_order
	Detail string `json:"detail"`
}

// Verify 校验案件证据链，检测缺失、篡改与乱序。返回空切片表示链完整。
func (s *Store) Verify(caseID uint) ([]Issue, error) {
	var events []models.ChainEvent
	if err := s.DB.Where("case_id = ?", caseID).Order("seq ASC").Find(&events).Error; err != nil {
		return nil, err
	}
	var issues []Issue
	prevHash := GenesisPrev
	var expectSeq uint64 = 1
	for _, ev := range events {
		if ev.Seq > expectSeq {
			for missing := expectSeq; missing < ev.Seq; missing++ {
				issues = append(issues, Issue{
					Seq:    missing,
					Kind:   "missing",
					Detail: fmt.Sprintf("event with seq %d is missing", missing),
				})
			}
		}
		if ev.Seq < expectSeq {
			issues = append(issues, Issue{
				Seq:    ev.Seq,
				Kind:   "out_of_order",
				Detail: fmt.Sprintf("duplicate or regressed seq %d (expected %d)", ev.Seq, expectSeq),
			})
		}
		if want := ContentHash(ev.Seq, ev.Type, ev.Actor, ev.Payload, ev.PrevHash, ev.CreatedAtUnix); want != ev.Hash {
			issues = append(issues, Issue{
				Seq:    ev.Seq,
				Kind:   "tampered",
				Detail: "content hash mismatch: stored fields do not match recorded hash",
			})
		}
		if ev.PrevHash != prevHash {
			issues = append(issues, Issue{
				Seq:    ev.Seq,
				Kind:   "out_of_order",
				Detail: "prev_hash does not match previous event hash: chain link broken",
			})
		}
		prevHash = ev.Hash
		expectSeq = ev.Seq + 1
	}
	return issues, nil
}

func isDuplicateKey(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	return strings.Contains(msg, "Duplicate entry") || // MySQL
		strings.Contains(msg, "UNIQUE constraint failed") // SQLite（测试）
}

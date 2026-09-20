// Package chain 实现案件维度的证据哈希链（append-only）。
//
// 每条事件是一个规范化信封：
//
//	{type, sequence, prev, actor, at, payload}
//
// digest = SHA256(规范化信封 JSON)；首事件 prev 为 64 个 '0'。
// 追加在“案件行锁 + 最后事件行锁”后进行，保证并发追加不会分叉
// （序号连续、每条 prev 都指向真实的最新摘要）。
//
// 局限：哈希链只能证明链内数据被改动/缺失/乱序，不能提供可信时间
// （at 由本服务时钟产生），也不能代替外部签名或时间戳服务。
package chain

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"gorm.io/gorm"

	"github.com/example/forensiccore/internal/model"
)

// ZeroDigest 是创世前驱摘要。
const ZeroDigest = "0000000000000000000000000000000000000000000000000000000000000000"

// 校验问题代码。
const (
	IssueGap        = "missing"      // 序号缺失
	IssueFirstPrev  = "bad_genesis"  // 首事件 prev 不是全 0
	IssueBrokenLink = "broken_link"  // prev 与前一事件摘要不符（分叉/乱序）
	IssueTampered   = "tampered"     // 事件摘要或规范化内容被改
	IssueOutOfOrder = "out_of_order" // 时间戳相对前一事件倒退
)

// Envelope 是参与摘要计算的规范化信封。
type Envelope struct {
	Type     string
	Sequence int64
	Prev     string
	Actor    string
	At       time.Time
	// Payload 必须是已经定型的 JSON 对象（由具体事件结构序列化而来）。
	Payload json.RawMessage
}

// CanonicalJSON 返回排序键后的规范化信封 JSON（紧凑，无多余空白）。
func (e Envelope) CanonicalJSON() ([]byte, error) {
	var payload any
	if len(e.Payload) > 0 {
		dec := json.NewDecoder(bytes.NewReader(e.Payload))
		dec.UseNumber()
		if err := dec.Decode(&payload); err != nil {
			return nil, fmt.Errorf("payload is not valid json: %w", err)
		}
	} else {
		payload = map[string]any{}
	}
	m := map[string]any{
		"type":     e.Type,
		"sequence": e.Sequence,
		"prev":     e.Prev,
		"actor":    e.Actor,
		"at":       e.At.UTC().Format(time.RFC3339Nano),
		"payload":  payload,
	}
	raw, err := json.Marshal(m) // map 的键按字典序输出
	if err != nil {
		return nil, err
	}
	return raw, nil
}

// Digest 计算规范化信封的大写 hex SHA-256。
func (e Envelope) Digest() (string, []byte, error) {
	raw, err := e.CanonicalJSON()
	if err != nil {
		return "", nil, err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), raw, nil
}

// ErrInvalidEventType 事件类型不在四类白名单中。
var ErrInvalidEventType = errors.New("invalid event type")

func validType(t string) bool {
	return t == model.EventRegister || t == model.EventVerify ||
		t == model.EventTransfer || t == model.EventNote
}

// AppendInput 是追加事件的输入。
type AppendInput struct {
	CaseID  uint
	Type    string
	Actor   string
	At      time.Time
	Payload json.RawMessage
}

// Append 在案件链尾部原子追加一条事件，返回写入的事件。
//
// 并发安全：先对该案件行加事务级锁（MySQL 行锁；SQLite 单连接串行），
// 再读取当前最后序号/摘要，因此并发提交被强制串行化，链不会分叉。
func Append(db *gorm.DB, in AppendInput) (*model.ChainEvent, error) {
	if !validType(in.Type) {
		return nil, ErrInvalidEventType
	}
	if in.At.IsZero() {
		in.At = time.Now().UTC()
	}

	var ev model.ChainEvent
	err := db.Transaction(func(tx *gorm.DB) error {
		// 锁定案件行：MySQL 下阻塞其它并发追加直到本事务提交；
		// SQLite 由单连接串行化保证。
		var c model.Case
		if err := lockCase(tx, in.CaseID).First(&c).Error; err != nil {
			return err
		}

		var last model.ChainEvent
		var tail []model.ChainEvent
		if err := tx.Where("case_id = ?", in.CaseID).
			Order("sequence DESC").Limit(1).Find(&tail).Error; err != nil {
			return err
		}
		if len(tail) == 0 {
			last = model.ChainEvent{Sequence: 0, Digest: ZeroDigest}
		} else {
			last = tail[0]
		}

		// 事件时间在案件锁内确定，保证相对前一事件单调不减：
		// 并发调用各自在事务外取的 time.Now 可能相等甚至（在不同时钟下）
		// 乱序，因此这里以链上最后时间为下界钳制，避免把正常并发误判为乱序。
		at := in.At.UTC()
		if at.IsZero() {
			at = time.Now().UTC()
		}
		if last.Sequence > 0 && at.Before(last.CreatedAt) {
			at = last.CreatedAt
		}

		env := Envelope{
			Type:     in.Type,
			Sequence: last.Sequence + 1,
			Prev:     last.Digest,
			Actor:    in.Actor,
			At:       at,
			Payload:  in.Payload,
		}
		digest, canonical, err := env.Digest()
		if err != nil {
			return err
		}
		ev = model.ChainEvent{
			CaseID:     in.CaseID,
			Sequence:   env.Sequence,
			EventType:  env.Type,
			Actor:      env.Actor,
			Canonical:  string(canonical),
			PrevDigest: env.Prev,
			Digest:     digest,
			CreatedAt:  env.At,
		}
		return tx.Create(&ev).Error
	})
	if err != nil {
		return nil, err
	}
	return &ev, nil
}

// Issue 描述校验中发现的一处链异常。
type Issue struct {
	Sequence int64  `json:"sequence"`
	Code     string `json:"code"`
	Message  string `json:"message"`
}

// Report 是链校验结果。
type Report struct {
	CaseID       uint    `json:"case_id"`
	EventCount   int     `json:"event_count"`
	Healthy      bool    `json:"healthy"`
	Issues       []Issue `json:"issues"`
	HeadSequence int64   `json:"head_sequence"`
	HeadDigest   string  `json:"head_digest,omitempty"`
}

// Verify 读取案件全部事件并校验缺失、分叉/乱序、篡改。
func Verify(db *gorm.DB, caseID uint) (*Report, error) {
	var events []model.ChainEvent
	if err := db.Where("case_id = ?", caseID).
		Order("sequence ASC").Find(&events).Error; err != nil {
		return nil, err
	}
	rep := &Report{CaseID: caseID, EventCount: len(events), Healthy: true}

	var prevDigest string
	var prevAt time.Time
	var prevSeq int64
	for i, ev := range events {
		if i == 0 {
			// 首事件必须是序号 1，prev 为全 0。
			if ev.Sequence != 1 {
				rep.Healthy = false
				rep.Issues = append(rep.Issues, Issue{
					Sequence: ev.Sequence, Code: IssueGap,
					Message: fmt.Sprintf("chain starts at sequence %d, events 1..%d are missing",
						ev.Sequence, ev.Sequence-1),
				})
			}
			if ev.PrevDigest != ZeroDigest {
				rep.Healthy = false
				rep.Issues = append(rep.Issues, Issue{
					Sequence: ev.Sequence, Code: IssueFirstPrev,
					Message: "first event prev digest must be all zeros",
				})
			}
			prevDigest = ZeroDigest
		} else {
			// 连续性（缺失检测）。
			if ev.Sequence != prevSeq+1 {
				rep.Healthy = false
				for missing := prevSeq + 1; missing < ev.Sequence; missing++ {
					rep.Issues = append(rep.Issues, Issue{
						Sequence: missing, Code: IssueGap,
						Message: fmt.Sprintf("event %d is missing", missing),
					})
				}
			}
			// 链接：prev 必须指向前一条现存事件摘要（分叉/乱序检测）。
			if ev.PrevDigest != prevDigest {
				rep.Healthy = false
				rep.Issues = append(rep.Issues, Issue{
					Sequence: ev.Sequence, Code: IssueBrokenLink,
					Message: fmt.Sprintf("prev digest does not match event %d digest", prevSeq),
				})
			}
		}

		// 内容篡改：规范化文本重序列化必须字节一致，且其摘要必须等于存储摘要。
		wantDigest, err := digestOfStored(ev.Canonical)
		if err != nil {
			rep.Healthy = false
			rep.Issues = append(rep.Issues, Issue{
				Sequence: ev.Sequence, Code: IssueTampered,
				Message: "canonical payload is not valid json",
			})
		} else {
			if !canonicalStable(ev.Canonical) {
				rep.Healthy = false
				rep.Issues = append(rep.Issues, Issue{
					Sequence: ev.Sequence, Code: IssueTampered,
					Message: "stored canonical content is not in canonical form (modified outside the service)",
				})
			}
			if wantDigest != ev.Digest {
				rep.Healthy = false
				rep.Issues = append(rep.Issues, Issue{
					Sequence: ev.Sequence, Code: IssueTampered,
					Message: "stored digest does not match the digest of canonical content",
				})
			}
		}

		// 时间乱序：同序号递增前提下，时间戳不得倒退。
		if i > 0 && ev.CreatedAt.Before(prevAt) {
			rep.Healthy = false
			rep.Issues = append(rep.Issues, Issue{
				Sequence: ev.Sequence, Code: IssueOutOfOrder,
				Message: "event timestamp is earlier than the previous event",
			})
		}

		prevDigest = ev.Digest
		prevSeq = ev.Sequence
		prevAt = ev.CreatedAt
	}

	if len(events) > 0 {
		rep.HeadSequence = events[len(events)-1].Sequence
		rep.HeadDigest = events[len(events)-1].Digest
	}
	return rep, nil
}

// digestOfStored 直接对存储的规范化字节求摘要。
func digestOfStored(canonical string) (string, error) {
	sum := sha256.Sum256([]byte(canonical))
	return hex.EncodeToString(sum[:]), nil
}

// canonicalStable 校验存储的规范化 JSON 重新确定性序列化后字节一致。
// 使用 json.Number 精确保留整数文本，避免大数被转成科学计数法。
func canonicalStable(canonical string) bool {
	dec := json.NewDecoder(bytes.NewReader([]byte(canonical)))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return false
	}
	out, err := json.Marshal(v)
	if err != nil {
		return false
	}
	return bytes.Equal(out, []byte(canonical))
}

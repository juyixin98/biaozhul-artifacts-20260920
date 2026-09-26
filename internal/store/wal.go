package store

import (
	"bufio"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
)

// walOp 是预写日志中的一条记录。
//
// 日志只有三种操作：
//   - pending：抢到幂等键，写入"处理中"占位（已 fsync，进程崩溃后仍可见）
//   - commit：一次性写入【执行结果 + 副作用】，二者共用同一个提交点
//   - release：执行失败/占位回收，删除占位（从未提交过副作用，重试安全）
type walOp struct {
	Op          string          `json:"op"`
	Key         string          `json:"key,omitempty"`
	Fingerprint string          `json:"fingerprint,omitempty"`
	Digest      string          `json:"digest,omitempty"`
	StatusCode  int             `json:"status_code,omitempty"`
	Response    json.RawMessage `json:"response,omitempty"`
	Effect      *Effect         `json:"effect,omitempty"`
	At          string          `json:"at,omitempty"`
	ExpiresAt   string          `json:"expires_at,omitempty"`
	Gen         uint64          `json:"gen,omitempty"`
}

func (s *Store) appendOp(op walOp) error {
	b, err := json.Marshal(op)
	if err != nil {
		return err
	}
	b = append(b, '\n')
	if _, err := s.wal.Write(b); err != nil {
		return err
	}
	// 每次落盘都 fsync：崩溃/断电后提交点明确（已 fsync 即生效，未 fsync 即不存在）。
	return s.wal.Sync()
}

// recover 从 WAL 重建内存状态。commit 记录在回放时才产生账本条目与余额变更，
// 因此"日志里有几条 commit，副作用就恰好有几次"——恢复过程本身也是幂等的。
func (s *Store) recover() error {
	path := filepath.Join(s.dir, walFileName)
	f, err := os.OpenFile(path, os.O_RDONLY, 0o600)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		return err
	}
	defer f.Close()

	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var op walOp
		if err := json.Unmarshal(line, &op); err != nil {
			return err
		}
		s.replayOp(op)
	}
	return sc.Err()
}

// replayOp 把单条日志操作应用到内存状态。
func (s *Store) replayOp(op walOp) {
	switch op.Op {
	case opPending:
		if old, ok := s.state.Keys[op.Key]; ok && old.Gen >= op.Gen {
			return // 旧代次日志不能覆盖新代次状态
		}
		if op.Gen > s.maxGen[op.Key] {
			s.maxGen[op.Key] = op.Gen
		}
		s.state.Keys[op.Key] = recordFromOp(op, Pending)
	case opCommit:
		r := recordFromOp(op, Completed)
		s.state.Keys[op.Key] = r
		if op.Effect != nil {
			s.state.Ledger = append(s.state.Ledger, *op.Effect)
			s.state.Balances[op.Effect.Account] += op.Effect.Amount
		}
	case opRelease:
		if cur, ok := s.state.Keys[op.Key]; ok && cur.Gen == op.Gen {
			delete(s.state.Keys, op.Key)
		}
	}
}

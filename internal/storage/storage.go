// Package storage 提供同步状态的持久化：已验证区块、可信检查点、
// 节点宣称状态以及完整的来源证据审计日志。所有写入均为同步 fsync，
// 进程崩溃 / 重启后只能从已提交的连续前缀恢复。
package storage

import (
	"database/sql"
	"encoding/hex"
	"fmt"

	"nodesync/internal/chain"

	_ "modernc.org/sqlite"
)

// Store 封装 SQLite 数据库。
type Store struct {
	db *sql.DB
}

// EvidenceEvent 是一条来源证据 / 同步事件。
type EvidenceEvent struct {
	ID         int64  `json:"id"`
	TS         string `json:"ts"`
	Event      string `json:"event"` // segment_verified | segment_rejected | peer_tip | checkpoint_advance | cancel ...
	HeightFrom int64  `json:"height_from"`
	HeightTo   int64  `json:"height_to"`
	NodeID     string `json:"node_id"`
	Detail     string `json:"detail"`
}

// PeerStatus 是某个远端节点最近一次的可观测状态。
type PeerStatus struct {
	NodeID           string
	AdvertisedHeight uint64
	AdvertisedHash   string
	LastContactTS    string
	LastError        string
}

// Open 打开（必要时创建）数据库并执行迁移。
func Open(path string) (*Store, error) {
	// _txlock=immediate 让写事务立即加锁，避免 SQLITE_BUSY 升级死锁。
	db, err := sql.Open("sqlite", path+"?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)&_pragma=synchronous(FULL)&_txlock=immediate")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1) // SQLite 单写者，串行化所有访问，简化事务语义
	s := &Store{db: db}
	if err := s.migrate(); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) migrate() error {
	_, err := s.db.Exec(`
CREATE TABLE IF NOT EXISTS blocks (
    height      INTEGER PRIMARY KEY,
    hash        BLOB NOT NULL,
    parent_hash BLOB NOT NULL,
    payload     BLOB NOT NULL,
    signature   BLOB NOT NULL,
    node_id     TEXT NOT NULL,
    verified_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    event       TEXT NOT NULL,
    height_from INTEGER NOT NULL,
    height_to   INTEGER NOT NULL,
    node_id     TEXT NOT NULL,
    detail      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS peer_status (
    node_id           TEXT PRIMARY KEY,
    advertised_height INTEGER NOT NULL,
    advertised_hash   TEXT NOT NULL,
    last_contact_ts   TEXT NOT NULL,
    last_error        TEXT NOT NULL DEFAULT ''
);
`)
	return err
}

// Close 关闭数据库。
func (s *Store) Close() error { return s.db.Close() }

// TipHeight 返回已验证并提交的最高区块高度；空库返回 ^0（即无区块）。
func (s *Store) TipHeight() (uint64, bool, error) {
	var h uint64
	err := s.db.QueryRow(`SELECT height FROM blocks ORDER BY height DESC LIMIT 1`).Scan(&h)
	if err == sql.ErrNoRows {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	return h, true, nil
}

// GetBlock 读取一个已存区块。
func (s *Store) GetBlock(height uint64) (*chain.Block, bool, error) {
	row := s.db.QueryRow(
		`SELECT parent_hash, payload, signature FROM blocks WHERE height = ?`, height)
	var parent, payload, sig []byte
	if err := row.Scan(&parent, &payload, &sig); err != nil {
		if err == sql.ErrNoRows {
			return nil, false, nil
		}
		return nil, false, err
	}
	return &chain.Block{Height: height, ParentHash: parent, Payload: payload, Signature: sig}, true, nil
}

// BlocksMap 返回所有已存区块（以高度为键），用于检查点校验。
func (s *Store) BlocksMap() (map[uint64]*chain.Block, error) {
	rows, err := s.db.Query(`SELECT height, parent_hash, payload, signature FROM blocks ORDER BY height`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[uint64]*chain.Block{}
	for rows.Next() {
		var h uint64
		var parent, payload, sig []byte
		if err := rows.Scan(&h, &parent, &payload, &sig); err != nil {
			return nil, err
		}
		out[h] = &chain.Block{Height: h, ParentHash: parent, Payload: payload, Signature: sig}
	}
	return out, rows.Err()
}

// AppendVerifiedSegment 在一个事务中追加"已经过密码学与链接校验"的连续区块段。
// blocks 必须从 expectStart 开始连续；expectStart=0 时第一段必须是创世块。
// 再度做一次数据库内的连续性防御检查（不信任调用方）。
func (s *Store) AppendVerifiedSegment(blocks []*chain.Block, expectStart uint64, nodeID string) error {
	if len(blocks) == 0 {
		return fmt.Errorf("空段不能提交")
	}
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	var anchorHash []byte
	if expectStart > 0 {
		row := tx.QueryRow(`SELECT hash FROM blocks WHERE height = ?`, expectStart-1)
		if err := row.Scan(&anchorHash); err != nil {
			return fmt.Errorf("缺口：高度 %d 的前驱未提交: %w", expectStart-1, err)
		}
	}

	for i, b := range blocks {
		expectH := expectStart + uint64(i)
		if b.Height != expectH {
			return fmt.Errorf("段内高度不连续: 期望 %d 得到 %d", expectH, b.Height)
		}
		if i == 0 && expectStart > 0 {
			if !eq(anchorHash, b.ParentHash) {
				return fmt.Errorf("高度 %d 父哈希与已提交链不一致", b.Height)
			}
		}
		if i > 0 && !eq(blocks[i-1].Hash(), b.ParentHash) {
			return fmt.Errorf("段内高度 %d 父哈希断裂", b.Height)
		}
		if _, err := tx.Exec(
			`INSERT INTO blocks(height, hash, parent_hash, payload, signature, node_id)
			 VALUES(?,?,?,?,?,?)`,
			b.Height, b.Hash(), b.ParentHash, b.Payload, b.Signature, nodeID); err != nil {
			return fmt.Errorf("写入高度 %d 失败: %w", b.Height, err)
		}
	}

	if err := tx.Commit(); err != nil {
		return err
	}
	return nil
}

// BlockProvenance 返回某高度区块的来源节点（证据）。
func (s *Store) BlockProvenance(height uint64) (string, error) {
	var node string
	err := s.db.QueryRow(`SELECT node_id FROM blocks WHERE height = ?`, height).Scan(&node)
	if err == sql.ErrNoRows {
		return "", nil
	}
	return node, err
}

// AddEvidence 追加一条证据事件。
func (s *Store) AddEvidence(ev EvidenceEvent) error {
	_, err := s.db.Exec(
		`INSERT INTO evidence(event, height_from, height_to, node_id, detail) VALUES(?,?,?,?,?)`,
		ev.Event, ev.HeightFrom, ev.HeightTo, ev.NodeID, ev.Detail)
	return err
}

// Evidence 返回全部证据事件（按时间顺序）。
func (s *Store) Evidence(limit int) ([]EvidenceEvent, error) {
	q := `SELECT id, ts, event, height_from, height_to, node_id, detail
	      FROM evidence ORDER BY id`
	args := []any{}
	if limit > 0 {
		q += ` LIMIT ?`
		args = append(args, limit)
	}
	rows, err := s.db.Query(q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []EvidenceEvent
	for rows.Next() {
		var e EvidenceEvent
		if err := rows.Scan(&e.ID, &e.TS, &e.Event, &e.HeightFrom, &e.HeightTo, &e.NodeID, &e.Detail); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// UpsertPeerStatus 更新某节点的宣称状态。
func (s *Store) UpsertPeerStatus(p PeerStatus) error {
	_, err := s.db.Exec(
		`INSERT INTO peer_status(node_id, advertised_height, advertised_hash, last_contact_ts, last_error)
		 VALUES(?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)
		 ON CONFLICT(node_id) DO UPDATE SET
		   advertised_height=excluded.advertised_height,
		   advertised_hash=excluded.advertised_hash,
		   last_contact_ts=excluded.last_contact_ts,
		   last_error=excluded.last_error`,
		p.NodeID, p.AdvertisedHeight, p.AdvertisedHash, p.LastError)
	return err
}

// AllPeerStatus 返回所有观测到的节点状态。
func (s *Store) AllPeerStatus() ([]PeerStatus, error) {
	rows, err := s.db.Query(
		`SELECT node_id, advertised_height, advertised_hash, last_contact_ts, last_error
		 FROM peer_status ORDER BY node_id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []PeerStatus
	for rows.Next() {
		var p PeerStatus
		if err := rows.Scan(&p.NodeID, &p.AdvertisedHeight, &p.AdvertisedHash, &p.LastContactTS, &p.LastError); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

// SetMeta / GetMeta 存储少量键值元数据。
func (s *Store) SetMeta(key, value string) error {
	_, err := s.db.Exec(
		`INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value`,
		key, value)
	return err
}

func (s *Store) GetMeta(key string) (string, error) {
	var v string
	err := s.db.QueryRow(`SELECT value FROM meta WHERE key=?`, key).Scan(&v)
	if err == sql.ErrNoRows {
		return "", nil
	}
	return v, err
}

// HashHex 是区块哈希的 hex 便捷函数。
func HashHex(b *chain.Block) string { return hex.EncodeToString(b.Hash()) }

func eq(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

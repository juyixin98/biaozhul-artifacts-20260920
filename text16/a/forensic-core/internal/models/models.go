// Package models 定义 ForensicCore 的持久化数据模型。
package models

import (
	"time"

	"gorm.io/gorm"
)

// Case 案件。
type Case struct {
	ID          uint      `gorm:"primaryKey" json:"id"`
	Name        string    `gorm:"size:255;uniqueIndex;not null" json:"name"`
	Description string    `gorm:"size:1024" json:"description"`
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"-"`
}

// Evidence 已登记的证据镜像基线。
type Evidence struct {
	ID           uint      `gorm:"primaryKey" json:"id"`
	CaseID       uint      `gorm:"index;not null" json:"case_id"`
	RelPath      string    `gorm:"size:1024;not null" json:"rel_path"` // 相对于证据根目录的路径
	Size         int64     `gorm:"not null" json:"size"`
	SHA256       string    `gorm:"size:64;index;not null" json:"sha256"`
	Dev          uint64    `gorm:"not null" json:"dev"`        // 文件所在设备号（文件身份）
	Ino          uint64    `gorm:"not null" json:"ino"`        // inode 号（文件身份）
	MtimeNsec    int64     `gorm:"not null" json:"mtime_nsec"` // 登记时的修改时间（纳秒）
	CtimeNsec    int64     `gorm:"not null" json:"ctime_nsec"` // 登记时的状态变更时间（纳秒）
	RegisteredBy string    `gorm:"size:255;not null" json:"registered_by"`
	CreatedAt    time.Time `json:"created_at"`
}

// ChunkHash 复核作业已处理分块的摘要，用于中断恢复前校验已处理部分未变化。
type ChunkHash struct {
	ID        uint   `gorm:"primaryKey" json:"-"`
	OwnerType string `gorm:"size:16;index:idx_chunk_owner,priority:1;not null" json:"owner_type"` // 固定为 "job"
	OwnerID   uint   `gorm:"index:idx_chunk_owner,priority:2;not null" json:"owner_id"`
	Seq       int    `gorm:"not null" json:"seq"`
	Offset    int64  `gorm:"not null" json:"offset"`
	Length    int64  `gorm:"not null" json:"length"`
	SHA256    string `gorm:"size:64;not null" json:"sha256"`
}

// 复核作业状态。
const (
	JobPending   = "pending"
	JobRunning   = "running"
	JobPaused    = "paused"
	JobCompleted = "completed"
	JobFailed    = "failed"
)

// 复核结论。
const (
	ResultNone     = ""
	ResultMatch    = "match"
	ResultMismatch = "mismatch"
)

// VerifyJob 完整性复核作业（持久化，可中断恢复）。
type VerifyJob struct {
	ID             uint       `gorm:"primaryKey" json:"id"`
	CaseID         uint       `gorm:"index;not null" json:"case_id"`
	EvidenceID     uint       `gorm:"index;not null" json:"evidence_id"`
	Status         string     `gorm:"size:16;index;not null" json:"status"`
	TotalSize      int64      `gorm:"not null" json:"total_size"`
	ProcessedBytes int64      `gorm:"not null" json:"processed_bytes"`
	Result         string     `gorm:"size:16;not null" json:"result"` // "" / match / mismatch
	Error          string     `gorm:"size:2048" json:"error,omitempty"`
	CreatedAt      time.Time  `json:"created_at"`
	UpdatedAt      time.Time  `json:"updated_at"`
	FinishedAt     *time.Time `json:"finished_at,omitempty"`
}

// 证据链事件类型（只追加）。
const (
	EventRegister = "register"
	EventVerify   = "verify"
	EventTransfer = "transfer"
	EventNote     = "note"
)

// ChainEvent 证据链事件。哈希覆盖规范化内容（含前一事件摘要），
// CreatedAtUnix 参与哈希计算以避免数据库时间精度往返问题。
type ChainEvent struct {
	ID            uint      `gorm:"primaryKey" json:"id"`
	CaseID        uint      `gorm:"uniqueIndex:idx_case_seq,priority:1;index;not null" json:"case_id"`
	Seq           uint64    `gorm:"uniqueIndex:idx_case_seq,priority:2;not null" json:"seq"`
	Type          string    `gorm:"size:16;not null" json:"type"`
	Actor         string    `gorm:"size:255;not null" json:"actor"`
	Payload       string    `gorm:"type:text;not null" json:"payload"` // 规范化 JSON
	PrevHash      string    `gorm:"size:64;not null" json:"prev_hash"`
	Hash          string    `gorm:"size:64;index;not null" json:"hash"`
	CreatedAtUnix int64     `gorm:"not null" json:"created_at_unix"`
	CreatedAt     time.Time `json:"created_at"`
}

// AutoMigrate 建表。
func AutoMigrate(db *gorm.DB) error {
	return db.AutoMigrate(&Case{}, &Evidence{}, &ChunkHash{}, &VerifyJob{}, &ChainEvent{})
}

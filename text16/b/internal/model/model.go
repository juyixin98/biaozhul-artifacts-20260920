package model

import "time"

// 案件状态。
const (
	CaseStatusOpen   = "open"
	CaseStatusClosed = "closed"
)

// 证据镜像登记/复核作业状态。
const (
	VerifyStatusQueued      = "queued"      // 已排队，等待执行
	VerifyStatusRunning     = "running"     // 执行中
	VerifyStatusInterrupted = "interrupted" // 中断，可恢复
	VerifyStatusVerified    = "verified"    // 复核通过
	VerifyStatusFailed      = "failed"      // 复核失败（文件被修改/身份不符等）
)

// 证据链事件类型（只追加，四类）。
const (
	EventRegister = "register" // 镜像登记（调查员）
	EventVerify   = "verify"   // 完整性复核结果
	EventTransfer = "transfer" // 移交保管（调查员）
	EventNote     = "note"     // 备注（调查员/分析师）
)

// Case 案件。证据链按案件维度组织。
type Case struct {
	ID         uint      `gorm:"primaryKey" json:"id"`
	CaseNumber string    `gorm:"size:128;uniqueIndex;not null" json:"case_number"`
	Title      string    `gorm:"size:256;not null" json:"title"`
	Status     string    `gorm:"size:32;not null;default:open" json:"status"`
	Custodian  string    `gorm:"size:128;not null" json:"custodian"`
	CreatedBy  string    `gorm:"size:128;not null" json:"created_by"`
	CreatedAt  time.Time `json:"created_at"`
	UpdatedAt  time.Time `json:"updated_at"`
}

// Evidence 登记的原始镜像（raw/dd）。原始文件永不被修改。
type Evidence struct {
	ID       uint   `gorm:"primaryKey" json:"id"`
	CaseID   uint   `gorm:"uniqueIndex:uk_evidence_case_name;not null" json:"case_id"`
	Name     string `gorm:"size:256;uniqueIndex:uk_evidence_case_name;not null" json:"name"`
	RootName string `gorm:"size:128;not null" json:"root_name"` // 白名单根逻辑名
	RelPath  string `gorm:"size:1024;not null" json:"rel_path"` // 相对根路径
	Size     int64  `gorm:"not null" json:"size"`
	SHA256   string `gorm:"size:64;not null" json:"sha256"` // 基线摘要（大写 hex）
	// 登记时文件身份（openat + fstat 得到），用于恢复时核对“是不是同一个文件”，
	// 而不是只看文件名/大小。
	FileDev      uint64    `gorm:"not null" json:"file_dev"`
	FileIno      uint64    `gorm:"not null" json:"file_ino"`
	RegisteredBy string    `gorm:"size:128;not null" json:"registered_by"`
	CreatedAt    time.Time `json:"created_at"`
	UpdatedAt    time.Time `json:"updated_at"`
}

// VerifyJob 持久化的完整性复核作业，携带可恢复进度。
type VerifyJob struct {
	ID            uint       `gorm:"primaryKey" json:"id"`
	EvidenceID    uint       `gorm:"index;not null" json:"evidence_id"`
	CaseID        uint       `gorm:"index;not null" json:"case_id"`
	Status        string     `gorm:"size:32;index;not null" json:"status"`
	ProcessedSize int64      `gorm:"not null;default:0" json:"processed_size"` // 已完成（已存块）字节数
	FinalSHA256   string     `gorm:"size:64" json:"final_sha256,omitempty"`
	LastError     string     `gorm:"type:text" json:"last_error,omitempty"`
	ChunkSize     int        `gorm:"not null" json:"chunk_size"`
	StartedBy     string     `gorm:"size:128;not null" json:"started_by"`
	StartedAt     time.Time  `json:"started_at"`
	FinishedAt    *time.Time `json:"finished_at,omitempty"`
	CreatedAt     time.Time  `json:"created_at"`
	UpdatedAt     time.Time  `json:"updated_at"`
}

// VerifyChunk 已完成块的独立摘要。恢复时逐块重算，验证“已处理部分”未被改动。
type VerifyChunk struct {
	ID        uint      `gorm:"primaryKey" json:"id"`
	JobID     uint      `gorm:"uniqueIndex:uk_chunk_job_offset;not null" json:"job_id"`
	Offset    int64     `gorm:"uniqueIndex:uk_chunk_job_offset;not null" json:"offset"`
	Length    int       `gorm:"not null" json:"length"`
	SHA256    string    `gorm:"size:64;not null" json:"sha256"`
	CreatedAt time.Time `json:"created_at"`
}

// ChainEvent 证据链事件（append-only）。
type ChainEvent struct {
	ID        uint   `gorm:"primaryKey" json:"id"`
	CaseID    uint   `gorm:"uniqueIndex:uk_chain_case_seq;index;not null" json:"case_id"`
	Sequence  int64  `gorm:"uniqueIndex:uk_chain_case_seq;not null" json:"sequence"` // 案件内连续序号
	EventType string `gorm:"size:32;not null" json:"event_type"`
	Actor     string `gorm:"size:128;not null" json:"actor"`
	// Canonical 是参与摘要计算的规范化信封 JSON（type/seq/prev/actor/at/payload）。
	Canonical  string `gorm:"type:text;not null" json:"canonical"`
	PrevDigest string `gorm:"size:64;not null" json:"prev_digest"` // 前一事件摘要；首事件为 64 个 0
	Digest     string `gorm:"size:64;not null" json:"digest"`      // 本事件规范化内容摘要
	// CreatedAt 即信封时间（参与链上时间顺序），显式写入，禁止 GORM 自动生成。
	CreatedAt time.Time `gorm:"autoCreateTime:false" json:"created_at"`
}

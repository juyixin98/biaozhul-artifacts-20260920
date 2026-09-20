package service

import "encoding/json"

// 以下 Payload 结构会被 JSON 序列化后放入哈希链信封。
// 字段名稳定、无多余键，保证摘要可复现；不要随意改字段名。

// RegisterPayload 登记事件内容。
type RegisterPayload struct {
	EvidenceID uint   `json:"evidence_id"`
	Name       string `json:"name"`
	RootName   string `json:"root_name"`
	RelPath    string `json:"rel_path"`
	Size       int64  `json:"size"`
	SHA256     string `json:"sha256"`
	FileDev    uint64 `json:"file_dev"`
	FileIno    uint64 `json:"file_ino"`
}

// VerifyPayload 复核事件内容。
type VerifyPayload struct {
	JobID       uint   `json:"job_id"`
	EvidenceID  uint   `json:"evidence_id"`
	Result      string `json:"result"` // verified / failed
	ExpectedSHA string `json:"expected_sha256"`
	ActualSHA   string `json:"actual_sha256,omitempty"`
	Size        int64  `json:"size"`
	Reason      string `json:"reason,omitempty"`
	Resumed     bool   `json:"resumed"`
}

// TransferPayload 移交事件内容。
type TransferPayload struct {
	EvidenceID uint   `json:"evidence_id"`
	From       string `json:"from"`
	To         string `json:"to"`
	Reason     string `json:"reason"`
}

// NotePayload 备注事件内容。
type NotePayload struct {
	EvidenceID uint   `json:"evidence_id,omitempty"`
	Text       string `json:"text"`
}

func mustJSON(v any) json.RawMessage {
	b, err := json.Marshal(v)
	if err != nil {
		// 所有载荷均为确定可序列化的简单结构。
		panic(err)
	}
	return b
}

package promotion

import "atomicpromo/internal/approval"

// PromoteRequest 晋级请求。所有引用都是不可变精确版本，不接受标签。
type PromoteRequest struct {
	Env            string  `json:"env"`
	Digest         string  `json:"digest"` // 必须 sha256:hex
	EvidenceID     string  `json:"evidence_id"`
	EvidenceVer    int64   `json:"evidence_version"`
	PolicyID       string  `json:"policy_id"`
	PolicyVer      int64   `json:"policy_version"`
	ExpectedGen    int64   `json:"expected_gen"` // 调用方看到的环境代次；不一致则拒绝
	ApprovalID     *string `json:"approval_id,omitempty"`
	IdempotencyKey *string `json:"idempotency_key,omitempty"`
}

type Stage string

const (
	StageValidate  Stage = "validate"
	StageCopy      Stage = "copy"
	StagePreCommit Stage = "pre_commit"
	StageCommit    Stage = "commit"
)

// Outcome 尝试结果
type Outcome struct {
	AttemptID string         `json:"attempt_id"`
	Status    string         `json:"status"` // committed | awaiting_approval | rejected | conflict | copy_failed
	Env       string         `json:"env"`
	Gen       *int64         `json:"gen,omitempty"`
	Reason    string         `json:"reason,omitempty"`
	Receipt   map[string]any `json:"receipt,omitempty"`
}

// RollbackRequest 回退请求：只能指向本环境历史中“完整且仍满足保留规则”的产物
type RollbackRequest struct {
	Env            string  `json:"env"`
	Digest         string  `json:"digest"` // 精确 digest；与 target_gen 二选一
	TargetGen      *int64  `json:"target_gen,omitempty"`
	ExpectedGen    int64   `json:"expected_gen"`
	PolicyID       string  `json:"policy_id"` // 用哪个策略版本重检保留规则
	PolicyVer      int64   `json:"policy_version"`
	IdempotencyKey *string `json:"idempotency_key,omitempty"`
}

// ApproveDecision 是 approval.Decision 的 HTTP 层别名
type ApproveDecision = approval.Decision

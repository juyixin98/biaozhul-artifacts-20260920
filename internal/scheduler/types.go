// Package scheduler 在给定物理拓扑（见 internal/topology）上完成
// GPU 多卡任务的放置决策。
//
// 决策分两阶段：
//  1. 硬约束过滤：每个副本独占一张卡，且该卡剩余显存必须
//     >= MemoryPerReplicaMB（设备可被多个任务共享显存）。
//  2. 在所有满足硬约束的设备子集中，最小化组内通信代价
//     （所有被分配设备两两之间 link cost 之和；同组通信按全归约
//     建模，每一对设备权重相等）。
//
// 小规模输入使用穷举保证全局最优；超过阈值时回退到
// 贪心 + 局部搜索的启发式算法（结果会显式标注 Exhaustive=false）。
package scheduler

// RejectReason 说明一个任务为什么无法被放置。
type RejectReason string

const (
	// ReasonNoCluster 尚未配置集群。
	ReasonNoCluster RejectReason = "NO_CLUSTER"
	// ReasonInvalidTask 任务请求参数非法（显存/卡数 <= 0 等）。
	ReasonInvalidTask RejectReason = "INVALID_TASK"
	// ReasonDuplicateTask 同名任务已存在。
	ReasonDuplicateTask RejectReason = "DUPLICATE_TASK"
	// ReasonNotEnoughDevices 集群设备总数小于 Replicas，结构性无解。
	ReasonNotEnoughDevices RejectReason = "NOT_ENOUGH_DEVICES"
	// ReasonFragmentedMemory 剩余显存总量够、设备数也够，
	// 但显存碎片化：单卡剩余显存足够装下一个副本的设备不足 Replicas 台。
	ReasonFragmentedMemory RejectReason = "FRAGMENTED_MEMORY"
	// ReasonInsufficientMemory 全集群剩余显存总和都放不下任务
	// （Replicas * MemoryPerReplicaMB）。
	ReasonInsufficientMemory RejectReason = "INSUFFICIENT_MEMORY"
)

// TaskRequest 是一次放置请求。
type TaskRequest struct {
	// TaskID 任务唯一标识，非空。
	TaskID string `json:"taskId"`
	// Replicas 需要的 GPU 数量，必须 > 0。
	Replicas int `json:"replicas"`
	// MemoryPerReplicaMB 每个副本（每张卡）需要的显存，必须 > 0。
	MemoryPerReplicaMB int `json:"memoryPerReplicaMB"`
}

// Assignment 描述任务的一个副本落在哪张卡上。
type Assignment struct {
	DeviceID      string `json:"deviceId"`
	NUMANode      int    `json:"numaNode"`
	AllocatedMB   int    `json:"allocatedMB"`
	UsedMemoryMB  int    `json:"usedMemoryMB"`
	FreeMemoryMB  int    `json:"freeMemoryMB"`
	TotalMemoryMB int    `json:"totalMemoryMB"`
}

// Placement 是一个任务的完整放置结果。
type Placement struct {
	TaskID      string       `json:"taskId"`
	Replicas    int          `json:"replicas"`
	Assignments []Assignment `json:"assignments"`
	// Cost 组内通信代价：被分配设备两两之间 link cost 之和。
	Cost int `json:"cost"`
	// SameNUMAPairs / CrossNUMAPairs 被分配设备中，处于相同 / 不同
	// NUMA 节点的设备对数，便于人工核验跨 NUMA 惩罚是否生效。
	SameNUMAPairs  int `json:"sameNUMAPairs"`
	CrossNUMAPairs int `json:"crossNUMAPairs"`
	// Exhaustive 为 true 表示该结果经穷举验证为全局最优。
	Exhaustive bool `json:"exhaustive"`
	// CandidatesEvaluated 求解过程评估的候选放置数量。
	CandidatesEvaluated int `json:"candidatesEvaluated"`
}

// Rejection 是可核验的拒绝结果。
type Rejection struct {
	TaskID string       `json:"taskId"`
	Reason RejectReason `json:"reason"`
	// Detail 人类可读的诊断信息。
	Detail string `json:"detail"`
	// Context 拒绝时刻的关键容量数据，方便调用方复核。
	Context RejectContext `json:"context"`
}

// RejectContext 记录拒绝时刻的容量快照。
type RejectContext struct {
	RequestedReplicas     int `json:"requestedReplicas"`
	MemoryPerReplicaMB    int `json:"memoryPerReplicaMB"`
	RequiredTotalMemoryMB int `json:"requiredTotalMemoryMB"`
	TotalDevices          int `json:"totalDevices"`
	EligibleDevices       int `json:"eligibleDevices"` // 单卡剩余显存足够装下一个副本
	TotalFreeMemoryMB     int `json:"totalFreeMemoryMB"`
	EligibleFreeMemoryMB  int `json:"eligibleFreeMemoryMB"`
}

// Decision 是一次 Allocate 的结果：Place 与 Reject 恰好一个非 nil。
type Decision struct {
	Place  *Placement `json:"place,omitempty"`
	Reject *Rejection `json:"reject,omitempty"`
}

// DeviceView 是集群当前状态的对外视图。
type DeviceView struct {
	DeviceID      string   `json:"deviceId"`
	NUMANode      int      `json:"numaNode"`
	TotalMemoryMB int      `json:"totalMemoryMB"`
	UsedMemoryMB  int      `json:"usedMemoryMB"`
	FreeMemoryMB  int      `json:"freeMemoryMB"`
	TaskIDs       []string `json:"taskIds,omitempty"` // 占用该设备的任务（可为多个）
}

// ClusterView 是 GET /api/cluster 的响应体。
type ClusterView struct {
	Configured bool         `json:"configured"`
	Devices    []DeviceView `json:"devices"`
	// PairCosts 设备两两之间的通信代价，键形如 "gpu0|gpu1"（id 字典序排列）。
	PairCosts        map[string]int `json:"pairCosts"`
	DefaultSameNUMA  int            `json:"defaultSameNUMACost"`
	DefaultCrossNUMA int            `json:"defaultCrossNUMACost"`
}

// ExhaustiveCandidateLimit 超过该候选规模时改用启发式求解。
// C(n,k) 上界；20 万组合在小集群（n<=20）下亚秒级可完成。
var ExhaustiveCandidateLimit = 200_000

func validateRequest(req TaskRequest) error {
	if req.TaskID == "" {
		return errInvalid("taskId 不能为空")
	}
	if req.Replicas <= 0 {
		return errInvalid("replicas 必须 > 0")
	}
	if req.MemoryPerReplicaMB <= 0 {
		return errInvalid("memoryPerReplicaMB 必须 > 0")
	}
	return nil
}

type validationError struct{ msg string }

func (e validationError) Error() string { return e.msg }

func errInvalid(msg string) error { return validationError{msg: msg} }

// IsValidationError 判断错误是否为请求参数校验错误。
func IsValidationError(err error) bool {
	_, ok := err.(validationError)
	return ok
}

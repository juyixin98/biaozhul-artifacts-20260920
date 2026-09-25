// Package scheduler 实现时间区间上的资源预约冲突求解。
//
// 核心约定：
//
//   - 时间使用整数离散刻度 Ticks（int64），含义由调用方决定
//     （例如“小时”“分钟”）。本库只做整数区间运算，不感知墙钟。
//   - 区间一律采用左闭右开的半开区间 [Start, End)：
//     区间 [1,3) 与 [3,5) 相邻但不重叠，在 t=3 处可被两段
//     预约同时“触碰”，互不冲突。
//   - 每种资源有多个容量维度（例如 [房间数, 席位数]），
//     预约给出等长的需求向量；同一时刻各维度上
//     活跃预约需求之和不得超过容量，容量为 0 的维度
//     不允许任何正需求。
//   - 一个预约必须占用一段连续时段（给定时长），不可拆分。
//   - 批量预约是原子的：任一条目与已存在预约或批次内
//     其它条目冲突，则整批拒绝，不产生任何状态变更。
package scheduler

import (
	"errors"
	"fmt"
)

// Ticks 是调度时间轴上的一个整数时刻。
type Ticks int64

// Interval 是左闭右开的整数时间区间 [Start, End)。
type Interval struct {
	Start Ticks `json:"start"`
	End   Ticks `json:"end"`
}

// Validate 检查区间是否合法：Start 必须严格小于 End。
// 零值或负长度区间视为无效。
func (iv Interval) Validate() error {
	if iv.Start >= iv.End {
		return fmt.Errorf("非法区间 [%d,%d)：开始时刻必须严格早于结束时刻", iv.Start, iv.End)
	}
	return nil
}

// Overlaps 判断两个半开区间是否重叠。
// 相邻区间（a.End == b.Start 或 b.End == a.Start）不算重叠。
func (iv Interval) Overlaps(other Interval) bool {
	return iv.Start < other.End && other.Start < iv.End
}

// Contains 判断时刻 t 是否落在区间内（左闭右开）。
func (iv Interval) Contains(t Ticks) bool {
	return iv.Start <= t && t < iv.End
}

// Demand 是一个预约在各容量维度上的需求向量。
type Demand []int64

// Resource 描述一种可预约资源及其多维容量。
//
// Capacity 的长度即维度数；所有针对该资源的预约需求向量
// 必须与之等长。容量必须非负，允许为 0。
type Resource struct {
	ID       string  `json:"id"`
	Name     string  `json:"name,omitempty"`
	Capacity []int64 `json:"capacity"`
}

// ReservationStatus 是预约的生命周期状态。
type ReservationStatus string

const (
	// StatusPending 已预约但占用时段尚未开始。
	StatusPending ReservationStatus = "pending"
	// StatusRunning 占用时段已经开始，且执行器未报告失败。
	StatusRunning ReservationStatus = "running"
	// StatusCompleted 占用时段正常结束。
	StatusCompleted ReservationStatus = "completed"
	// StatusFailed 执行器在开始时报告失败，预约提前终止。
	StatusFailed ReservationStatus = "failed"
	// StatusCancelled 被用户取消（仅 pending/running 可取消）。
	StatusCancelled ReservationStatus = "cancelled"
)

// IsTerminal 判断状态是否为终态。
func (s ReservationStatus) IsTerminal() bool {
	return s == StatusCompleted || s == StatusFailed || s == StatusCancelled
}

// Request 描述一条固定区间的预约请求。
type Request struct {
	// ID 为调用方指定的预约 ID；为空时由系统生成。
	// 非空时必须全局唯一。
	ID       string   `json:"id,omitempty"`
	Resource string   `json:"resource"`
	Interval Interval `json:"interval"`
	// Demand 为各容量维度上的需求量，长度必须与资源容量一致。
	Demand Demand `json:"demand"`
	// Meta 为调用方附带的不透明元数据（仅存储/回显，不参与求解）。
	Meta map[string]string `json:"meta,omitempty"`
}

// PlacementRequest 描述一条“只给时长、让系统找位置”的预约请求。
type PlacementRequest struct {
	ID       string            `json:"id,omitempty"`
	Resource string            `json:"resource"`
	Window   Interval          `json:"window"`
	Duration Ticks             `json:"duration"`
	Demand   Demand            `json:"demand"`
	Earliest Ticks             `json:"earliest,omitempty"`
	Meta     map[string]string `json:"meta,omitempty"`
}

// Reservation 是一条已落地的预约记录。
type Reservation struct {
	ID       string            `json:"id"`
	Resource string            `json:"resource"`
	Interval Interval          `json:"interval"`
	Demand   Demand            `json:"demand"`
	Status   ReservationStatus `json:"status"`
	Meta     map[string]string `json:"meta,omitempty"`
	// AutoPlaced 标记该预约的区间是否由系统“最早可行位置”自动放置。
	AutoPlaced bool `json:"auto_placed,omitempty"`
}

// 业务错误码（结构化错误的 Code 字段）。
const (
	// CodeInvalidRequest 请求本身非法（字段缺失、向量维度不符等）。
	CodeInvalidRequest = "invalid_request"
	// CodeNotFound 资源或预约不存在。
	CodeNotFound = "not_found"
	// CodeConflict 容量冲突、ID 冲突、状态不允许当前操作等。
	CodeConflict = "conflict"
	// CodeNoFeasibleSlot 窗口内不存在满足需求的连续时段。
	CodeNoFeasibleSlot = "no_feasible_slot"
	// CodeTerminal 预约已处于终态，操作无意义。
	CodeTerminal = "terminal"
)

// Violation 描述一个维度在某一时段上的容量超限明细。
type Violation struct {
	// Segment 为发生超限的半开区间。
	Segment Interval `json:"segment"`
	// Dimension 为超限的维度下标。
	Dimension int `json:"dimension"`
	// Load 为该时段内该维度上的总需求。
	Load int64 `json:"load"`
	// Capacity 为资源在该维度上的容量。
	Capacity int64 `json:"capacity"`
}

// Error 是调度库返回的结构化错误。
//
// HTTP 层可据 Code 映射状态码；冲突类错误还携带
// 造成冲突的已存在预约 ID 列表与逐维度的 Violation 明细，
// 便于调用方定位原因。
type Error struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	// Conflicts 为参与冲突的已存在预约 ID（已去重、已排序）。
	Conflicts []string `json:"conflicts,omitempty"`
	// Violations 为逐时段逐维度的超限明细。
	Violations []Violation `json:"violations,omitempty"`
}

func (e *Error) Error() string {
	if len(e.Conflicts) > 0 {
		return fmt.Sprintf("%s: %s（冲突预约: %v）", e.Code, e.Message, e.Conflicts)
	}
	return e.Code + ": " + e.Message
}

func invalid(format string, args ...any) error {
	return &Error{Code: CodeInvalidRequest, Message: fmt.Sprintf(format, args...)}
}

func notFound(format string, args ...any) error {
	return &Error{Code: CodeNotFound, Message: fmt.Sprintf(format, args...)}
}

func conflict(msg string, ids []string, violations []Violation) error {
	return &Error{Code: CodeConflict, Message: msg, Conflicts: ids, Violations: violations}
}

func noFeasible(msg string) error {
	return &Error{Code: CodeNoFeasibleSlot, Message: msg}
}

// AsError 从任意 error 中提取结构化 *Error；不是时返回 nil。
func AsError(err error) *Error {
	var e *Error
	if errors.As(err, &e) {
		return e
	}
	return nil
}

// RejectItemError 是批量预约中针对单个条目的拒绝原因。
type RejectItemError struct {
	// Index 为该条目在请求批次中的下标（从 0 开始）。
	Index int `json:"index"`
	// ID 为条目自带的预约 ID（可能为空）。
	ID string `json:"id,omitempty"`
	// Err 为结构化错误原因。
	Err *Error `json:"error"`
}

// BatchError 表示整批预约被拒绝；不携带任何部分落地结果。
type BatchError struct {
	// Total 为批次中的条目总数。
	Total int `json:"total"`
	// Items 列出所有被发现有问题的条目（至少一条）。
	Items []RejectItemError `json:"items"`
}

func (b *BatchError) Error() string {
	return fmt.Sprintf("批量预约被拒绝：%d/%d 个条目存在问题", len(b.Items), b.Total)
}

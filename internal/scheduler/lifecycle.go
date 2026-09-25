package scheduler

import (
	"fmt"
	"sort"
)

// Get 返回预约记录副本。
func (s *Scheduler) Get(id string) (*Reservation, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.byID[id]
	if !ok {
		return nil, notFound("预约 %q 不存在", id)
	}
	return cloneReservation(r), nil
}

// ListFilter 过滤预约列表。零值字段表示不过滤。
type ListFilter struct {
	Resource string
	Status   ReservationStatus
}

// List 返回（按开始时刻、ID 排序的）预约列表，可按资源与状态过滤。
func (s *Scheduler) List(f ListFilter) []*Reservation {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]*Reservation, 0, len(s.byID))
	for _, r := range s.byID {
		if f.Resource != "" && r.Resource != f.Resource {
			continue
		}
		if f.Status != "" && r.Status != f.Status {
			continue
		}
		out = append(out, cloneReservation(r))
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Interval.Start != out[j].Interval.Start {
			return out[i].Interval.Start < out[j].Interval.Start
		}
		return out[i].ID < out[j].ID
	})
	return out
}

func cloneReservation(r *Reservation) *Reservation {
	cp := *r
	cp.Demand = append(Demand(nil), r.Demand...)
	cp.Meta = cloneMeta(r.Meta)
	return &cp
}

// Cancel 取消一条仍占用容量的预约（pending/running）。
// 已处于终态的预约不可取消；取消后立即释放其容量，
// 新的预约可以立刻使用相邻/重叠区间。
func (s *Scheduler) Cancel(id string, at Ticks) (*Reservation, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.byID[id]
	if !ok {
		return nil, notFound("预约 %q 不存在", id)
	}
	if r.Status.IsTerminal() {
		return nil, &Error{
			Code:    CodeTerminal,
			Message: fmt.Sprintf("预约 %q 已处于终态 %s，不能取消", id, r.Status),
		}
	}
	st := s.resources[r.Resource]
	s.removeActiveLocked(st, r)
	r.Status = StatusCancelled
	s.events.Emit(EventReservationCancelled, CancelledDetail{
		ID: id, Resource: r.Resource, At: at,
	})
	return cloneReservation(r), nil
}

// removeActiveLocked 从资源的活动列表中删除预约。调用方持锁。
func (s *Scheduler) removeActiveLocked(st *resState, r *Reservation) {
	for i, x := range st.active {
		if x.ID == r.ID {
			st.active = append(st.active[:i], st.active[i+1:]...)
			return
		}
	}
}

// AdvanceResult 是一次时间推进的结果。
type AdvanceResult struct {
	Started   []string // 本 tick 内 pending -> running 的预约 ID
	Completed []string // 本 tick 内 running/pending -> completed 的预约 ID
}

// Advance 把调度时钟推进到 now：
//
//   - 所有 Start <= now 且仍 pending 的预约转为 running；
//   - 所有 End <= now 的非终态预约转为 completed 并释放容量。
//
// 同一次推进内先处理开始、再处理结束：在 tick 粒度上，
// 半开区间 [Start,End) 在 End 处已不再占用容量。
//
// 该方法只做状态迁移与事件记录，不调用执行器；执行器的
// 失败报告走 MarkFailed。dispatcher 负责把两者编排起来。
//
// 注意：pending 预约若在创建时其 Start 已经早于当前调度时间，
// 行为取决于调用方何时调用 Advance——通常创建只面向未来。
func (s *Scheduler) Advance(now Ticks) AdvanceResult {
	s.mu.Lock()
	defer s.mu.Unlock()

	var res AdvanceResult

	// 1) 开始：Start <= now 的 pending 预约。
	var starting []*Reservation
	for _, resID := range s.resOrder {
		st := s.resources[resID]
		for _, r := range st.active {
			if r.Status == StatusPending && r.Interval.Start <= now {
				starting = append(starting, r)
			}
		}
	}
	sort.Slice(starting, func(i, j int) bool {
		if starting[i].Interval.Start != starting[j].Interval.Start {
			return starting[i].Interval.Start < starting[j].Interval.Start
		}
		return starting[i].ID < starting[j].ID
	})
	for _, r := range starting {
		r.Status = StatusRunning
		res.Started = append(res.Started, r.ID)
		s.events.Emit(EventReservationStarted, StartedDetail{
			ID: r.ID, Resource: r.Resource, Interval: r.Interval,
		})
	}

	// 2) 结束：End <= now 的所有非终态预约。
	var ending []*Reservation
	for _, resID := range s.resOrder {
		st := s.resources[resID]
		for _, r := range st.active {
			if !r.Status.IsTerminal() && r.Interval.End <= now {
				ending = append(ending, r)
			}
		}
	}
	sort.Slice(ending, func(i, j int) bool {
		if ending[i].Interval.End != ending[j].Interval.End {
			return ending[i].Interval.End < ending[j].Interval.End
		}
		return ending[i].ID < ending[j].ID
	})
	for _, r := range ending {
		s.removeActiveLocked(s.resources[r.Resource], r)
		r.Status = StatusCompleted
		res.Completed = append(res.Completed, r.ID)
		s.events.Emit(EventReservationCompleted, StartedDetail{
			ID: r.ID, Resource: r.Resource, Interval: r.Interval,
		})
	}
	return res
}

// MarkFailed 由执行器调用：报告一条正在运行的预约失败。
//
// 预约立即转为 failed 终态并释放容量。重复报告或对终态预约
// 报告均返回 conflict 错误（不重复发事件）。
func (s *Scheduler) MarkFailed(id, reason string) (*Reservation, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.byID[id]
	if !ok {
		return nil, notFound("预约 %q 不存在", id)
	}
	if r.Status.IsTerminal() {
		return nil, &Error{
			Code:    CodeTerminal,
			Message: fmt.Sprintf("预约 %q 已处于终态 %s，不能标记失败", id, r.Status),
		}
	}
	s.removeActiveLocked(s.resources[r.Resource], r)
	r.Status = StatusFailed
	s.events.Emit(EventReservationFailed, FailedDetail{
		StartedDetail: StartedDetail{
			ID: r.ID, Resource: r.Resource, Interval: r.Interval,
		},
		Reason: reason,
	})
	return cloneReservation(r), nil
}

// DueStarts 返回在时刻 now 应当开始（Start <= now）但仍 pending
// 的预约 ID。供不想使用 Advance 的外部执行器自行编排。
func (s *Scheduler) DueStarts(now Ticks) []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	var ids []string
	for _, resID := range s.resOrder {
		for _, r := range s.resources[resID].active {
			if r.Status == StatusPending && r.Interval.Start <= now {
				ids = append(ids, r.ID)
			}
		}
	}
	sort.Strings(ids)
	return ids
}

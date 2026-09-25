package scheduler

import (
	"fmt"
	"sort"
	"sync"
	"sync/atomic"

	"resourcebooking/internal/clock"
)

// resState 是资源在调度器内部的运行时状态。
type resState struct {
	res *Resource
	// active 为当前非终态预约（pending+running），按开始时刻有序。
	active []*Reservation
}

// Scheduler 是线程安全的资源预约求解器。
//
// 它维护资源表、预约表与结构化事件日志，提供：
//
//   - 固定区间预约 Reserve
//   - 最早可行位置查询 EarliestFeasible
//   - 原子批量预约 Batch
//   - 取消 Cancel
//   - 时间推进（供 dispatcher 调用的 Advance 生命周期方法）
type Scheduler struct {
	mu        sync.Mutex
	resources map[string]*resState
	resOrder  []string // 资源注册顺序
	// byID 预约 ID -> 预约；active 中的指针与 byID 指向同一对象。
	byID map[string]*Reservation
	seq  atomic.Int64

	events *EventLog
}

// New 创建调度器。events 为 nil 时使用内存事件日志与系统墙钟。
func New(events *EventLog) *Scheduler {
	if events == nil {
		events = NewEventLog(clock.Wall{})
	}
	return &Scheduler{
		resources: map[string]*resState{},
		byID:      map[string]*Reservation{},
		events:    events,
	}
}

// Events 返回事件日志句柄。
func (s *Scheduler) Events() *EventLog { return s.events }

// AddResource 注册一种资源。容量维度数固定后不可更改；
// 容量各分量必须非负（允许为 0）。
func (s *Scheduler) AddResource(r Resource) (*Resource, error) {
	if r.ID == "" {
		return nil, invalid("资源 ID 不能为空")
	}
	if len(r.Capacity) == 0 {
		return nil, invalid("资源 %q 必须至少声明一个容量维度", r.ID)
	}
	for i, c := range r.Capacity {
		if c < 0 {
			return nil, invalid("资源 %q 的维度 %d 容量为负: %d", r.ID, i, c)
		}
	}
	stored := &Resource{ID: r.ID, Name: r.Name, Capacity: append([]int64(nil), r.Capacity...)}

	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.resources[r.ID]; ok {
		return nil, conflict(fmt.Sprintf("资源 %q 已存在", r.ID), nil, nil)
	}
	s.resources[r.ID] = &resState{res: stored}
	s.resOrder = append(s.resOrder, r.ID)
	// EventLog 使用独立的锁且订阅推送非阻塞，持调度器锁发事件是安全的。
	// 发送资源快照，保证事件是不可变的事实记录。
	s.events.Emit(EventResourceAdded, cloneResource(stored))
	return stored, nil
}

// GetResource 返回资源定义（容量为副本）。
func (s *Scheduler) GetResource(id string) (*Resource, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	st, ok := s.resources[id]
	if !ok {
		return nil, notFound("资源 %q 不存在", id)
	}
	return cloneResource(st.res), nil
}

// ListResources 按注册顺序返回全部资源。
func (s *Scheduler) ListResources() []*Resource {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]*Resource, 0, len(s.resOrder))
	for _, id := range s.resOrder {
		out = append(out, cloneResource(s.resources[id].res))
	}
	return out
}

func cloneResource(r *Resource) *Resource {
	return &Resource{ID: r.ID, Name: r.Name, Capacity: append([]int64(nil), r.Capacity...)}
}

// validateDemand 校验需求向量并返回与资源对齐的副本。
func validateDemand(st *resState, d Demand) (Demand, error) {
	if len(d) != len(st.res.Capacity) {
		return nil, invalid("资源 %q 有 %d 个容量维度，但需求向量长度为 %d",
			st.res.ID, len(st.res.Capacity), len(d))
	}
	cp := make(Demand, len(d))
	for i, v := range d {
		if v < 0 {
			return nil, invalid("资源 %q 的维度 %d 需求为负: %d", st.res.ID, i, v)
		}
		if v > st.res.Capacity[i] {
			return nil, invalid("资源 %q 的维度 %d 单条需求 %d 超过容量 %d",
				st.res.ID, i, v, st.res.Capacity[i])
		}
		cp[i] = v
	}
	return cp, nil
}

// overlapActive 返回与 iv 重叠（半开语义）的非终态预约。
// 调用方持锁。
func overlapActive(st *resState, iv Interval) []*Reservation {
	var out []*Reservation
	for _, r := range st.active {
		if r.Interval.Overlaps(iv) {
			out = append(out, r)
		}
	}
	return out
}

// loadSegment 是参与扫描线的一个占用段；id 为空表示候选 op 自身。
type loadSegment struct {
	id     string
	iv     Interval
	demand Demand
}

// checkItem 用扫描线方法检查“在 st 上再放一段 op”是否可行。
//
// op 为候选预约（尚未入库，ID 可能为空）；existing 为需要一并
// 计入的其它未入库候选（批次内条目），只取同一资源且重叠的段。
// 返回冲突时 *Error 携带：
//
//   - Conflicts：在某个违规段内实际承载负载的已存在/批次内预约 ID；
//   - Violations：逐时段逐维度的超载明细。
//
// 扫描线以各占用段端点构造 +/- 事件，同一时刻先处理结束再处理
// 开始，使相邻段（端点相接）在公共端点处先释放后占用——这正是
// 半开区间 [Start,End) 的语义。
//
// 调用方持锁。
func checkItem(st *resState, op *Request, existing []*Reservation) *Error {
	capv := st.res.Capacity
	segs := make([]loadSegment, 0, len(st.active)+len(existing)+1)
	addIfOverlap := func(id string, iv Interval, d Demand) {
		if iv.Overlaps(op.Interval) {
			segs = append(segs, loadSegment{id, iv, d})
		}
	}
	// op 自身是整个候选区间上的固定负载（id 为空）。
	addIfOverlap("", op.Interval, op.Demand)
	for _, r := range st.active {
		addIfOverlap(r.ID, r.Interval, r.Demand)
	}
	for _, r := range existing {
		if r.Resource == st.res.ID {
			addIfOverlap(r.ID, r.Interval, r.Demand)
		}
	}

	type ev struct {
		t   Ticks
		add bool
		seg loadSegment
	}
	events := make([]ev, 0, len(segs)*2)
	for _, x := range segs {
		events = append(events, ev{x.iv.Start, true, x}, ev{x.iv.End, false, x})
	}
	sort.Slice(events, func(i, j int) bool {
		if events[i].t != events[j].t {
			return events[i].t < events[j].t
		}
		// 同一时刻先结束（-）后开始（+）。
		return !events[i].add && events[j].add
	})

	load := make(Demand, len(capv))
	// activeSegs 为当前扫描位置上承载负载的占用段。
	var activeSegs []loadSegment
	var violations []Violation
	conflictSet := map[string]struct{}{}
	prev := op.Interval.Start

	noteViolation := func(lo, hi Ticks) {
		for dim := range capv {
			if load[dim] <= capv[dim] {
				continue
			}
			violations = append(violations, Violation{
				Segment:   Interval{lo, hi},
				Dimension: dim,
				Load:      load[dim],
				Capacity:  capv[dim],
			})
			for _, sg := range activeSegs {
				if sg.id != "" && sg.demand[dim] > 0 {
					conflictSet[sg.id] = struct{}{}
				}
			}
		}
	}

	for _, e := range events {
		// 应用 e 处变化前，区间 [prev, e.t) 上负载恒为 load。
		if e.t > prev {
			lo, hi := prev, e.t
			if lo < op.Interval.Start {
				lo = op.Interval.Start
			}
			if hi > op.Interval.End {
				hi = op.Interval.End
			}
			if lo < hi {
				noteViolation(lo, hi)
			}
		}
		if e.add {
			for dim := range capv {
				load[dim] += e.seg.demand[dim]
			}
			activeSegs = append(activeSegs, e.seg)
		} else {
			for dim := range capv {
				load[dim] -= e.seg.demand[dim]
			}
			for i, sg := range activeSegs {
				if sg.id == e.seg.id && sg.iv == e.seg.iv {
					activeSegs = append(activeSegs[:i], activeSegs[i+1:]...)
					break
				}
			}
		}
		prev = e.t
	}

	if len(violations) == 0 {
		return nil
	}
	ids := make([]string, 0, len(conflictSet))
	for id := range conflictSet {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return conflict(fmt.Sprintf("资源 %s 在区间 [%d,%d) 上容量不足",
		st.res.ID, op.Interval.Start, op.Interval.End), ids, violations).(*Error)
}

// nextID 生成预约 ID：r-<单调序号>。调用方持锁。
func (s *Scheduler) nextID() string {
	return fmt.Sprintf("r-%d", s.seq.Add(1))
}

// commitLocked 把已构造好的预约放入索引并按开始时刻有序插入活动列表。
// 调用方持锁。
func (s *Scheduler) commitLocked(st *resState, r *Reservation) {
	s.byID[r.ID] = r
	idx := sort.Search(len(st.active), func(i int) bool {
		if st.active[i].Interval.Start != r.Interval.Start {
			return st.active[i].Interval.Start > r.Interval.Start
		}
		return st.active[i].ID > r.ID
	})
	st.active = append(st.active, nil)
	copy(st.active[idx+1:], st.active[idx:])
	st.active[idx] = r
}

func cloneMeta(m map[string]string) map[string]string {
	if len(m) == 0 {
		return nil
	}
	out := make(map[string]string, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

// Reserve 创建一条固定区间预约。冲突、非法或 ID 重复时返回错误，
// 不产生任何状态变更。
func (s *Scheduler) Reserve(req Request) (*Reservation, error) {
	if req.Resource == "" {
		return s.rejectFixed(req, invalid("资源 ID 不能为空"))
	}
	if err := req.Interval.Validate(); err != nil {
		return s.rejectFixed(req, invalid("%v", err))
	}

	s.mu.Lock()
	st, ok := s.resources[req.Resource]
	if !ok {
		s.mu.Unlock()
		return s.rejectFixed(req, notFound("资源 %q 不存在", req.Resource))
	}
	demand, err := validateDemand(st, req.Demand)
	if err != nil {
		s.mu.Unlock()
		return s.rejectFixed(req, err)
	}
	if req.ID != "" {
		if _, dup := s.byID[req.ID]; dup {
			s.mu.Unlock()
			return s.rejectFixed(req, conflict(fmt.Sprintf("预约 ID %q 已存在", req.ID), nil, nil))
		}
	}
	op := &Request{
		ID:       req.ID,
		Resource: req.Resource,
		Interval: req.Interval,
		Demand:   demand,
		Meta:     cloneMeta(req.Meta),
	}
	if cerr := checkItem(st, op, nil); cerr != nil {
		s.mu.Unlock()
		return s.rejectFixed(req, cerr)
	}
	id := op.ID
	if id == "" {
		id = s.nextID()
	}
	r := &Reservation{
		ID:       id,
		Resource: op.Resource,
		Interval: op.Interval,
		Demand:   op.Demand,
		Status:   StatusPending,
		Meta:     op.Meta,
	}
	s.commitLocked(st, r)
	// 发快照而非内部指针：事件是不可变事实，后续状态迁移
	// （running/completed/cancelled）不得回改历史事件负载。
	s.events.Emit(EventReservationCreated, cloneReservation(r))
	s.mu.Unlock()
	return r, nil
}

func (s *Scheduler) rejectFixed(req Request, err error) (*Reservation, error) {
	s.events.Emit(EventReservationRejected, RejectedDetail{
		RequestID: req.ID, Resource: req.Resource, Error: AsError(err),
	})
	return nil, err
}

// BatchItem 是批量预约的单个条目：二选一——
//
//   - 给出 Fixed 时按固定区间申请；
//   - 给出 Place 时在窗口内自动放置到最早可行位置。
type BatchItem struct {
	Fixed *Request          `json:"fixed,omitempty"`
	Place *PlacementRequest `json:"place,omitempty"`
}

// Batch 原子地创建一批预约。
//
// 整批在同一把调度锁内完成“字段校验 + 逐条求解 + 统一提交”，
// 因此批次看到的是一致快照，且不会与并发请求产生
// Time-Of-Check/Time-Of-Use 竞态。固定条目直接做容量检查，
// 放置条目取“相对已存在活动预约与批次内已选段”的最早可行
// 连续时段。任一条目不满足则整批拒绝（*BatchError），不落地
// 任何条目、不改变既有状态，并发出 batch_rejected 审计事件。
func (s *Scheduler) Batch(items []BatchItem) ([]*Reservation, error) {
	if len(items) == 0 {
		return nil, invalid("批量预约不能为空")
	}

	// prepared 为通过字段校验后的条目内部表示。
	type prepared struct {
		id       string
		st       *resState
		demand   Demand
		meta     map[string]string
		iv       Interval // fixed：最终区间
		auto     bool     // 是否自动放置
		window   Interval // place：搜索窗口
		duration Ticks    // place：时长
		earliest Ticks    // place：最早起点
	}
	prep := make([]*prepared, len(items))
	var rejects []RejectItemError
	batchIDs := map[string]struct{}{}

	s.mu.Lock()
	defer s.mu.Unlock()

	// 阶段 1：字段校验、资源解析、ID 去重（全局 + 批次内）。
	for i, it := range items {
		if (it.Fixed == nil) == (it.Place == nil) {
			rejects = append(rejects, RejectItemError{
				Index: i,
				Err:   invalid("每个批次条目必须且只能给出 fixed 或 place 之一").(*Error),
			})
			continue
		}
		var (
			p  = &prepared{}
			id string
		)
		if it.Fixed != nil {
			f := it.Fixed
			id = f.ID
			p.iv, p.demand, p.meta = f.Interval, f.Demand, cloneMeta(f.Meta)
			if err := f.Interval.Validate(); err != nil {
				rejects = append(rejects, RejectItemError{Index: i, ID: id, Err: invalid("%v", err).(*Error)})
				continue
			}
			var ok bool
			p.st, ok = s.resources[f.Resource]
			if !ok {
				rejects = append(rejects, RejectItemError{Index: i, ID: id, Err: notFound("资源 %q 不存在", f.Resource).(*Error)})
				continue
			}
		} else {
			q := it.Place
			id = q.ID
			p.auto = true
			p.window, p.duration, p.earliest = q.Window, q.Duration, q.Earliest
			p.demand, p.meta = q.Demand, cloneMeta(q.Meta)
			if err := q.Window.Validate(); err != nil {
				rejects = append(rejects, RejectItemError{Index: i, ID: id, Err: invalid("放置窗口非法: %v", err).(*Error)})
				continue
			}
			if q.Duration <= 0 {
				rejects = append(rejects, RejectItemError{Index: i, ID: id, Err: invalid("自动放置时长必须为正整数，得到 %d", q.Duration).(*Error)})
				continue
			}
			if q.Duration > q.Window.End-q.Window.Start {
				rejects = append(rejects, RejectItemError{Index: i, ID: id, Err: noFeasible(fmt.Sprintf(
					"时长 %d 超过窗口长度 %d", q.Duration, q.Window.End-q.Window.Start)).(*Error)})
				continue
			}
			var ok bool
			p.st, ok = s.resources[q.Resource]
			if !ok {
				rejects = append(rejects, RejectItemError{Index: i, ID: id, Err: notFound("资源 %q 不存在", q.Resource).(*Error)})
				continue
			}
		}
		d, verr := validateDemand(p.st, p.demand)
		if verr != nil {
			rejects = append(rejects, RejectItemError{Index: i, ID: id, Err: AsError(verr)})
			continue
		}
		p.demand = d
		if id != "" {
			if _, dupGlobal := s.byID[id]; dupGlobal {
				rejects = append(rejects, RejectItemError{Index: i, ID: id, Err: conflict(fmt.Sprintf("预约 ID %q 已存在", id), nil, nil).(*Error)})
				continue
			}
			if _, dupBatch := batchIDs[id]; dupBatch {
				rejects = append(rejects, RejectItemError{Index: i, ID: id, Err: conflict(fmt.Sprintf("预约 ID %q 在批次内重复", id), nil, nil).(*Error)})
				continue
			}
			batchIDs[id] = struct{}{}
		}
		p.id = id
		prep[i] = p
	}

	if rejects != nil {
		s.events.Emit(EventReservationBatchRejected, BatchRejectedDetail{Items: rejects})
		return nil, &BatchError{Total: len(items), Items: rejects}
	}

	// 阶段 2：逐条求解（与已存在活动预约及批次内已选段一起检查）。
	chosen := make([]*Reservation, 0, len(items))
	for i, p := range prep {
		op := &Request{ID: p.id, Resource: p.st.res.ID, Demand: p.demand, Meta: p.meta}
		var itemErr *Error
		if !p.auto {
			op.Interval = p.iv
			itemErr = checkItem(p.st, op, chosen)
		} else {
			start, found := earliestLocked(p.st, p.window, p.duration, p.earliest, p.demand, chosen)
			if !found {
				itemErr = noFeasible(fmt.Sprintf("资源 %q 在窗口 [%d,%d) 内找不到长度 %d 的可行连续时段",
					p.st.res.ID, p.window.Start, p.window.End, p.duration)).(*Error)
			} else {
				op.Interval = Interval{start, start + p.duration}
			}
		}
		if itemErr != nil {
			rejects = append(rejects, RejectItemError{Index: i, ID: p.id, Err: itemErr})
			continue
		}
		id := p.id
		if id == "" {
			id = s.nextID()
		}
		chosen = append(chosen, &Reservation{
			ID:         id,
			Resource:   op.Resource,
			Interval:   op.Interval,
			Demand:     op.Demand,
			Status:     StatusPending,
			Meta:       op.Meta,
			AutoPlaced: p.auto,
		})
	}

	if len(rejects) > 0 {
		// 关键：chosen 全部丢弃，不提交任何条目。
		s.events.Emit(EventReservationBatchRejected, BatchRejectedDetail{Items: rejects})
		return nil, &BatchError{Total: len(items), Items: rejects}
	}

	// 阶段 3：全部可行，统一提交（纯内存写入，不会失败）。
	for _, r := range chosen {
		s.commitLocked(s.resources[r.Resource], r)
	}
	ids := make([]string, len(chosen))
	snapshots := make([]*Reservation, len(chosen))
	for i, r := range chosen {
		ids[i] = r.ID
		snapshots[i] = cloneReservation(r)
	}
	s.events.Emit(EventReservationBatchCreated, BatchCreatedDetail{
		IDs: ids, Count: len(chosen), Items: snapshots,
	})
	return chosen, nil
}

// EarliestFeasible 在窗口内返回使给定需求可行的最早开始时刻。
//
// 搜索范围为 [max(window.Start, earliest), window.End-duration]。
// 半开区间语义下，可行/不可行的分界只会出现在“已有占用的结束
// 时刻”（占用结束即释放容量；占用开始不会新释放任何容量），
// 因此候选起点取 window 起点与各重叠占用的结束时刻即可完备。
// 窗口内不存在可行位置时 ok=false（这不是错误）。
func (s *Scheduler) EarliestFeasible(resource string, window Interval, duration Ticks, earliest Ticks, demand Demand) (start Ticks, ok bool, err error) {
	if err := window.Validate(); err != nil {
		return 0, false, invalid("%v", err)
	}
	if duration <= 0 {
		return 0, false, invalid("时长必须为正整数，得到 %d", duration)
	}
	if duration > window.End-window.Start {
		return 0, false, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	st, exists := s.resources[resource]
	if !exists {
		return 0, false, notFound("资源 %q 不存在", resource)
	}
	d, verr := validateDemand(st, demand)
	if verr != nil {
		return 0, false, verr
	}
	t, found := earliestLocked(st, window, duration, earliest, d, nil)
	return t, found, nil
}

// earliestLocked 为最早可行位置搜索的内部实现。
// extra 为批次内已选但尚未入库的候选段。调用方持锁。
func earliestLocked(st *resState, window Interval, duration, earliest Ticks, demand Demand, extra []*Reservation) (Ticks, bool) {
	lo := window.Start
	if earliest > lo {
		lo = earliest
	}
	if lo < window.Start || duration > window.End-lo {
		return 0, false
	}
	cand := lo
	op := &Request{Resource: st.res.ID, Demand: demand, Interval: Interval{cand, cand + duration}}
	if checkItem(st, op, extra) == nil {
		return cand, true
	}
	const sentinel = Ticks(1 << 62)
	for {
		next := sentinel
		consider := func(r *Reservation) {
			if r.Interval.End > cand && r.Interval.End < next &&
				r.Interval.Overlaps(Interval{cand, cand + duration}) {
				next = r.Interval.End
			}
		}
		for _, r := range st.active {
			consider(r)
		}
		for _, r := range extra {
			if r.Resource == st.res.ID {
				consider(r)
			}
		}
		if next == sentinel {
			return 0, false
		}
		cand = next
		if cand < lo || cand+duration > window.End {
			return 0, false
		}
		op.Interval = Interval{cand, cand + duration}
		if checkItem(st, op, extra) == nil {
			return cand, true
		}
	}
}

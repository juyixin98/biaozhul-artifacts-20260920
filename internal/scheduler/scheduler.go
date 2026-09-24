package scheduler

import (
	"fmt"
	"sort"
	"sync"

	"github.com/example/gpu-placement/internal/topology"
)

// Scheduler 是线程安全的放置服务状态：一份不可变拓扑 + 每张卡的显存占用记录。
//
// 设备可被多个任务共享（显存累加不超过总量即可），这与真实 GPU 集群
// 的显存切分一致，也使“显存碎片”成为可观测、可测试的一等现象。
type Scheduler struct {
	mu sync.RWMutex

	cluster *topology.Cluster
	// used[i] 为设备 i 上已分配的显存（MB）。
	used []int
	// tasks 保存已放置任务的结果，按 taskId 索引。
	tasks map[string]*Placement
}

// New 创建空调度器，随后通过 Configure 载入拓扑。
func New() *Scheduler {
	return &Scheduler{tasks: make(map[string]*Placement)}
}

// Configure 用新拓扑整体替换当前配置。要求没有任何在运行的任务，
// 否则返回错误（应先释放全部任务再重配，避免悬挂分配）。
func (s *Scheduler) Configure(spec topology.Spec) error {
	cl, err := topology.Build(spec)
	if err != nil {
		return err
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.tasks) > 0 {
		return fmt.Errorf("集群中仍有 %d 个在运行任务，请先全部释放后再重配", len(s.tasks))
	}

	s.cluster = cl
	s.used = make([]int, cl.N())
	s.tasks = make(map[string]*Placement)
	return nil
}

// HasCluster 报告是否已成功配置拓扑。
func (s *Scheduler) HasCluster() bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.cluster != nil
}

// Allocate 执行一次放置决策。
//
// 决策顺序（拒绝原因的优先级）：
//  1. 参数非法 / 重名任务：INVALID_TASK / DUPLICATE_TASK
//  2. 设备总数 < Replicas：NOT_ENOUGH_DEVICES
//  3. 全集群剩余显存总和 < 需求总量：INSUFFICIENT_MEMORY
//  4. 单卡剩余显存足够装下一个副本的设备数 < Replicas：FRAGMENTED_MEMORY
//     （总量够、但显存散落在多张小卡上，无法按副本整卡放下）
func (s *Scheduler) Allocate(req TaskRequest) Decision {
	if err := validateRequest(req); err != nil {
		return reject(req, ReasonInvalidTask, err.Error(), RejectContext{
			RequestedReplicas:     req.Replicas,
			MemoryPerReplicaMB:    req.MemoryPerReplicaMB,
			RequiredTotalMemoryMB: req.Replicas * req.MemoryPerReplicaMB,
		})
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	if s.cluster == nil {
		return reject(req, ReasonNoCluster, "尚未配置集群，请先调用 PUT /api/cluster", RejectContext{
			RequestedReplicas:     req.Replicas,
			MemoryPerReplicaMB:    req.MemoryPerReplicaMB,
			RequiredTotalMemoryMB: req.Replicas * req.MemoryPerReplicaMB,
		})
	}
	if _, exists := s.tasks[req.TaskID]; exists {
		return reject(req, ReasonDuplicateTask,
			fmt.Sprintf("任务 %q 已存在，请先释放后再以同名重新申请", req.TaskID), s.snapshotContextLocked(req))
	}

	n := s.cluster.N()
	requiredTotal := req.Replicas * req.MemoryPerReplicaMB

	var eligible []int
	totalFree, eligibleFree := 0, 0
	for i := 0; i < n; i++ {
		free := s.cluster.MemoryMB(i) - s.used[i]
		totalFree += free
		if free >= req.MemoryPerReplicaMB {
			eligible = append(eligible, i)
			eligibleFree += free
		}
	}

	ctx := RejectContext{
		RequestedReplicas:     req.Replicas,
		MemoryPerReplicaMB:    req.MemoryPerReplicaMB,
		RequiredTotalMemoryMB: requiredTotal,
		TotalDevices:          n,
		EligibleDevices:       len(eligible),
		TotalFreeMemoryMB:     totalFree,
		EligibleFreeMemoryMB:  eligibleFree,
	}

	if n < req.Replicas {
		return reject(req, ReasonNotEnoughDevices,
			fmt.Sprintf("集群共 %d 台设备，任务需要 %d 台（每个副本独占一张卡）", n, req.Replicas), ctx)
	}
	if totalFree < requiredTotal {
		return reject(req, ReasonInsufficientMemory,
			fmt.Sprintf("集群剩余显存合计 %dMB，但任务需要 %dMB（%d 副本 × %dMB/副本）",
				totalFree, requiredTotal, req.Replicas, req.MemoryPerReplicaMB), ctx)
	}
	if len(eligible) < req.Replicas {
		return reject(req, ReasonFragmentedMemory,
			fmt.Sprintf("总量充足但显存碎片化：仅 %d 台设备单卡剩余 >= %dMB，任务需要 %d 台",
				len(eligible), req.MemoryPerReplicaMB, req.Replicas), ctx)
	}

	// 硬约束全部满足，在 eligible 上最小化组内通信代价。
	result := solve(s.cluster, eligible, req.Replicas)

	assignments := make([]Assignment, 0, len(result.idx))
	for _, i := range result.idx {
		s.used[i] += req.MemoryPerReplicaMB
		assignments = append(assignments, Assignment{
			DeviceID:      s.cluster.DeviceID(i),
			NUMANode:      s.cluster.NUMANode(i),
			AllocatedMB:   req.MemoryPerReplicaMB,
			UsedMemoryMB:  s.used[i],
			FreeMemoryMB:  s.cluster.MemoryMB(i) - s.used[i],
			TotalMemoryMB: s.cluster.MemoryMB(i),
		})
	}
	// 设备顺序按 id 字典序输出，结果可核验、可复现。
	sort.Slice(assignments, func(a, b int) bool {
		return assignments[a].DeviceID < assignments[b].DeviceID
	})

	placement := &Placement{
		TaskID:              req.TaskID,
		Replicas:            req.Replicas,
		Assignments:         assignments,
		Cost:                result.cost,
		SameNUMAPairs:       result.samePairs,
		CrossNUMAPairs:      result.crossPairs,
		Exhaustive:          result.exhaustive,
		CandidatesEvaluated: result.evaluated,
	}
	s.tasks[req.TaskID] = placement
	return Decision{Place: placement}
}

// Release 释放任务占用的全部显存。任务不存在时返回 false。
func (s *Scheduler) Release(taskID string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, ok := s.tasks[taskID]
	if !ok {
		return false
	}
	for _, a := range p.Assignments {
		idx, ok := s.cluster.Index(a.DeviceID)
		if !ok {
			continue // 拓扑未变时不会发生；防御性处理
		}
		s.used[idx] -= a.AllocatedMB
		if s.used[idx] < 0 {
			s.used[idx] = 0
		}
	}
	delete(s.tasks, taskID)
	return true
}

// Task 返回某个已放置任务的快照。
func (s *Scheduler) Task(taskID string) (*Placement, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	p, ok := s.tasks[taskID]
	if !ok {
		return nil, false
	}
	cp := *p
	cp.Assignments = append([]Assignment(nil), p.Assignments...)
	return &cp, true
}

// Tasks 返回全部已放置任务，按 taskId 字典序排列。
func (s *Scheduler) Tasks() []Placement {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]Placement, 0, len(s.tasks))
	for _, p := range s.tasks {
		cp := *p
		cp.Assignments = append([]Assignment(nil), p.Assignments...)
		out = append(out, cp)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].TaskID < out[j].TaskID })
	return out
}

// View 返回当前集群的完整对外视图；未配置时 Configured=false。
func (s *Scheduler) View() ClusterView {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.cluster == nil {
		return ClusterView{Configured: false, PairCosts: map[string]int{}}
	}
	cl := s.cluster

	devs := make([]DeviceView, 0, cl.N())
	for i := 0; i < cl.N(); i++ {
		total := cl.MemoryMB(i)
		devs = append(devs, DeviceView{
			DeviceID:      cl.DeviceID(i),
			NUMANode:      cl.NUMANode(i),
			TotalMemoryMB: total,
			UsedMemoryMB:  s.used[i],
			FreeMemoryMB:  total - s.used[i],
			TaskIDs:       s.tasksOnDeviceLocked(i),
		})
	}
	sort.Slice(devs, func(i, j int) bool { return devs[i].DeviceID < devs[j].DeviceID })

	costs := make(map[string]int)
	sortedIDs := cl.SortedIDs()
	for a := 0; a < len(sortedIDs); a++ {
		for b := a + 1; b < len(sortedIDs); b++ {
			ia, _ := cl.Index(sortedIDs[a])
			ib, _ := cl.Index(sortedIDs[b])
			costs[sortedIDs[a]+"|"+sortedIDs[b]] = cl.Cost(ia, ib)
		}
	}

	return ClusterView{
		Configured:       true,
		Devices:          devs,
		PairCosts:        costs,
		DefaultSameNUMA:  cl.SameNUMADefault(),
		DefaultCrossNUMA: cl.CrossNUMADefault(),
	}
}

// tasksOnDeviceLocked 返回占用设备 i 的任务 id 列表（升序）。调用方需持锁。
func (s *Scheduler) tasksOnDeviceLocked(i int) []string {
	id := s.cluster.DeviceID(i)
	var out []string
	for _, p := range s.tasks {
		for _, a := range p.Assignments {
			if a.DeviceID == id {
				out = append(out, p.TaskID)
				break
			}
		}
	}
	sort.Strings(out)
	return out
}

func (s *Scheduler) snapshotContextLocked(req TaskRequest) RejectContext {
	n := s.cluster.N()
	ctx := RejectContext{
		RequestedReplicas:     req.Replicas,
		MemoryPerReplicaMB:    req.MemoryPerReplicaMB,
		RequiredTotalMemoryMB: req.Replicas * req.MemoryPerReplicaMB,
		TotalDevices:          n,
	}
	for i := 0; i < n; i++ {
		free := s.cluster.MemoryMB(i) - s.used[i]
		ctx.TotalFreeMemoryMB += free
		if free >= req.MemoryPerReplicaMB {
			ctx.EligibleDevices++
			ctx.EligibleFreeMemoryMB += free
		}
	}
	return ctx
}

func reject(req TaskRequest, reason RejectReason, detail string, ctx RejectContext) Decision {
	return Decision{Reject: &Rejection{
		TaskID:  req.TaskID,
		Reason:  reason,
		Detail:  detail,
		Context: ctx,
	}}
}

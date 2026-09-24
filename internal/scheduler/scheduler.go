// Package scheduler 实现双资源（CPU、内存）的主导份额公平（Dominant Resource
// Fairness, DRF）调度器。
//
// 任务不可拆分（discrete / indivisible tasks）：任务要么整体得到请求的全部资源，
// 要么继续在队列中等待。调度器绝不超卖：任何时刻所有租户已分配资源之和不超过
// 集群容量，每个租户已分配资源不超过自己的配额。
package scheduler

import (
	"errors"
	"fmt"
	"math/big"
	"sort"
	"strings"
	"sync"
)

// Resources 是一组资源量。CPU 单位为 millicore（1 核 = 1000m），
// Mem 单位为 MiB。使用整数，避免浮点误差导致的资源守恒问题。
type Resources struct {
	CPU int64 `json:"cpu"`
	Mem int64 `json:"mem"`
}

// Quota 是租户可持有的资源上限（绝对配额，hard cap）。
type Quota = Resources

// 任务与租户的状态。
const (
	StatusRunning = "running"
	StatusQueued  = "queued"
)

// 排队任务无法被放置的原因。
const (
	// 资源不足：等其他租户释放资源后可能变得可放置。
	ReasonClusterCPU = "insufficient_cluster_cpu"
	ReasonClusterMem = "insufficient_cluster_mem"
	ReasonQuotaCPU   = "tenant_quota_cpu_exceeded"
	ReasonQuotaMem   = "tenant_quota_mem_exceeded"
	// 任务自身请求超过集群容量或租户配额，在配置不变的前提下永远无法放置。
	ReasonTaskTooLarge = "task_exceeds_cluster_capacity_or_quota"
	// 队列前方有任务（head-of-line blocking）：同一租户严格 FIFO。
	ReasonWaitingInQueue = "waiting_behind_head_of_queue"
)

// 领域错误，HTTP 层据此映射状态码。
var (
	ErrNotFound       = errors.New("not found")
	ErrDuplicateID    = errors.New("id already exists")
	ErrInvalidInput   = errors.New("invalid input")
	ErrTenantHasTasks = errors.New("tenant still has running or queued tasks")
)

// TenantSpec 是创建租户的参数。
type TenantSpec struct {
	ID     string
	Weight *big.Rat // 必须为正数；nil 视为 1
	Quota  *Quota   // nil 表示配额等于集群容量
}

// TaskSpec 是提交任务的参数。
type TaskSpec struct {
	ID     string
	Tenant string
	Demand Resources
}

// TaskView 是任务在快照中的对外表示。
type TaskView struct {
	ID            string `json:"id"`
	Tenant        string `json:"tenant"`
	Status        string `json:"status"`
	CPU           int64  `json:"cpu"`
	Mem           int64  `json:"mem"`
	SubmittedSeq  int64  `json:"submitted_seq"`
	DominantShare string `json:"dominant_share"` // 主导份额，精确分数
	BlockedReason string `json:"blocked_reason,omitempty"`
}

// TenantView 是租户在快照中的对外表示。
type TenantView struct {
	ID               string     `json:"id"`
	Weight           string     `json:"weight"`         // 精确分数
	WeightDecimal    string     `json:"weight_decimal"` // 保留 6 位小数
	Quota            Quota      `json:"quota"`
	Allocated        Resources  `json:"allocated"`
	NumRunning       int        `json:"num_running"`
	NumQueued        int        `json:"num_queued"`
	DominantResource string     `json:"dominant_resource"` // cpu | mem | ""
	DominantShare    string     `json:"dominant_share"`    // 实际主导份额，精确分数
	WeightedShare    string     `json:"weighted_share"`    // 主导份额/权重，精确分数
	WeightedDecimal  string     `json:"weighted_share_decimal"`
	Running          []TaskView `json:"running"`
	Queued           []TaskView `json:"queued"`
}

// StateView 是整个调度器的快照。
type StateView struct {
	Capacity   Resources    `json:"capacity"`
	Free       Resources    `json:"free"`
	Used       Resources    `json:"used"`
	NumRunning int          `json:"num_running"`
	NumQueued  int          `json:"num_queued"`
	Tenants    []TenantView `json:"tenants"` // 按 ID 字典序
}

type task struct {
	id     string
	tenant string
	demand Resources
	seq    int64 // 全局提交序号，只在租户内用作 FIFO 次序
	status string
}

type tenant struct {
	id      string
	weight  *big.Rat
	quota   Quota
	alloc   Resources
	running []*task
	queued  []*task
}

// Scheduler 是并发安全的 DRF 调度器。
type Scheduler struct {
	mu       sync.Mutex
	capacity Resources
	tenants  map[string]*tenant
	order    []string // 租户创建顺序（调度选择只按份额与 ID，此字段仅辅助遍历）
	tasks    map[string]*task
	taskSeq  int64
}

// New 创建调度器。容量两个维度都必须为正数。
func New(capacity Resources) (*Scheduler, error) {
	if capacity.CPU <= 0 || capacity.Mem <= 0 {
		return nil, fmt.Errorf("%w: cluster capacity cpu and mem must be positive", ErrInvalidInput)
	}
	return &Scheduler{
		capacity: capacity,
		tenants:  make(map[string]*tenant),
		tasks:    make(map[string]*task),
	}, nil
}

// Capacity 返回集群容量。
func (s *Scheduler) Capacity() Resources {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.capacity
}

// CreateTenant 创建租户。
func (s *Scheduler) CreateTenant(spec TenantSpec) error {
	if strings.TrimSpace(spec.ID) == "" {
		return fmt.Errorf("%w: tenant id is required", ErrInvalidInput)
	}
	w := spec.Weight
	if w == nil {
		w = big.NewRat(1, 1)
	}
	if w.Sign() <= 0 {
		return fmt.Errorf("%w: weight must be positive", ErrInvalidInput)
	}
	if spec.Quota != nil {
		q := *spec.Quota
		if q.CPU <= 0 || q.Mem <= 0 {
			return fmt.Errorf("%w: quota cpu and mem must be positive", ErrInvalidInput)
		}
		if q.CPU > s.capacity.CPU || q.Mem > s.capacity.Mem {
			return fmt.Errorf("%w: quota (%d,%d) must not exceed cluster capacity (%d,%d)",
				ErrInvalidInput, q.CPU, q.Mem, s.capacity.CPU, s.capacity.Mem)
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.tenants[spec.ID]; ok {
		return fmt.Errorf("%w: tenant %q", ErrDuplicateID, spec.ID)
	}
	q := s.capacity
	if spec.Quota != nil {
		q = *spec.Quota
	}
	s.tenants[spec.ID] = &tenant{
		id:     spec.ID,
		weight: new(big.Rat).Set(w),
		quota:  q,
	}
	s.order = append(s.order, spec.ID)
	return nil
}

// SubmitTask 提交任务。即使当前无法放置也会被接受并入队（status=queued）。
// 任务请求量必须为正数，且不得超过集群容量与租户配额，否则视为永久不可放置，
// 返回包装了 ErrInvalidInput 的错误，不进入系统。
func (s *Scheduler) SubmitTask(spec TaskSpec) (status string, err error) {
	if strings.TrimSpace(spec.ID) == "" {
		return "", fmt.Errorf("%w: task id is required", ErrInvalidInput)
	}
	if spec.Demand.CPU <= 0 || spec.Demand.Mem <= 0 {
		return "", fmt.Errorf("%w: task demand cpu and mem must be positive", ErrInvalidInput)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	t, ok := s.tenants[spec.Tenant]
	if !ok {
		return "", fmt.Errorf("%w: tenant %q", ErrNotFound, spec.Tenant)
	}
	if _, dup := s.tasks[spec.ID]; dup {
		return "", fmt.Errorf("%w: task %q", ErrDuplicateID, spec.ID)
	}
	if spec.Demand.CPU > s.capacity.CPU || spec.Demand.Mem > s.capacity.Mem {
		return "", fmt.Errorf("%w: task %q demand (%d cpu, %d mem) exceeds cluster capacity (%d, %d)",
			ErrInvalidInput, spec.ID, spec.Demand.CPU, spec.Demand.Mem, s.capacity.CPU, s.capacity.Mem)
	}
	if spec.Demand.CPU > t.quota.CPU || spec.Demand.Mem > t.quota.Mem {
		return "", fmt.Errorf("%w: task %q demand (%d cpu, %d mem) exceeds tenant %q quota (%d, %d)",
			ErrInvalidInput, spec.ID, spec.Demand.CPU, spec.Demand.Mem, t.id, t.quota.CPU, t.quota.Mem)
	}
	s.taskSeq++
	tk := &task{id: spec.ID, tenant: spec.Tenant, demand: spec.Demand, seq: s.taskSeq, status: StatusQueued}
	s.tasks[tk.id] = tk
	t.queued = append(t.queued, tk)
	s.scheduleLocked()
	return tk.status, nil
}

// ReleaseTask 释放（完成/删除）一个任务，并立即触发重调度。
func (s *Scheduler) ReleaseTask(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	tk, ok := s.tasks[id]
	if !ok {
		return fmt.Errorf("%w: task %q", ErrNotFound, id)
	}
	owner := s.tenants[tk.tenant]
	if owner == nil {
		// 理论不可达：tasks 里有记录却找不到属主。
		return fmt.Errorf("%w: task %q owner missing (internal error)", ErrNotFound, id)
	}
	if tk.status == StatusRunning {
		owner.alloc.CPU -= tk.demand.CPU
		owner.alloc.Mem -= tk.demand.Mem
		owner.running = removeTask(owner.running, id)
	} else {
		owner.queued = removeTask(owner.queued, id)
	}
	delete(s.tasks, id)
	s.scheduleLocked()
	return nil
}

// DeleteTenant 删除一个没有任何任务（运行中或排队中）的租户。
func (s *Scheduler) DeleteTenant(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	t, ok := s.tenants[id]
	if !ok {
		return fmt.Errorf("%w: tenant %q", ErrNotFound, id)
	}
	if len(t.running)+len(t.queued) > 0 {
		return fmt.Errorf("%w: tenant %q has %d running and %d queued tasks; release them first",
			ErrTenantHasTasks, id, len(t.running), len(t.queued))
	}
	delete(s.tenants, id)
	for i, x := range s.order {
		if x == id {
			s.order = append(s.order[:i], s.order[i+1:]...)
			break
		}
	}
	return nil
}

// Reset 清空全部租户与任务（演示/测试用）。
func (s *Scheduler) Reset() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.tenants = make(map[string]*tenant)
	s.order = nil
	s.tasks = make(map[string]*task)
	s.taskSeq = 0
}

// fitLocked 判断任务在当前已用量与租户配额下能否放置，并返回阻塞原因（空串=可放置）。
func (s *Scheduler) fitLocked(t *tenant, tk *task) string {
	if tk.demand.CPU > s.capacity.CPU-totalUsed(s, 0) {
		return ReasonClusterCPU
	}
	if tk.demand.Mem > s.capacity.Mem-totalUsed(s, 1) {
		return ReasonClusterMem
	}
	if tk.demand.CPU > t.quota.CPU-t.alloc.CPU {
		return ReasonQuotaCPU
	}
	if tk.demand.Mem > t.quota.Mem-t.alloc.Mem {
		return ReasonQuotaMem
	}
	return ""
}

// totalUsed 求所有租户某一维度的已分配总量。dim: 0=CPU, 1=Mem。
func totalUsed(s *Scheduler, dim int) int64 {
	var sum int64
	for _, t := range s.tenants {
		if dim == 0 {
			sum += t.alloc.CPU
		} else {
			sum += t.alloc.Mem
		}
	}
	return sum
}

// scheduleLocked 执行一遍 DRF 放置循环。
//
// 每轮：在“队列非空”的租户中挑选加权主导份额
// （max(alloc.cpu/cap.cpu, alloc.mem/cap.mem) / weight）最小者；
// 份额相同则按租户 ID 字典序（确定性平局规则）。若选中的队首任务放得下就放置，
// 否则本轮跳过它继续尝试下一个租户——只要还有任何租户放得下，放置就会继续；
// 当所有非空队列的队首都无法放置时停止。
func (s *Scheduler) scheduleLocked() {
	for {
		candidates := make([]*tenant, 0, len(s.tenants))
		for _, id := range s.order {
			if t := s.tenants[id]; len(t.queued) > 0 {
				candidates = append(candidates, t)
			}
		}
		if len(candidates) == 0 {
			return
		}
		sort.Slice(candidates, func(i, j int) bool {
			return lessTenant(candidates[i], candidates[j], s.capacity)
		})
		placed := false
		for _, t := range candidates {
			head := t.queued[0]
			if reason := s.fitLocked(t, head); reason != "" {
				continue
			}
			// 放置：队首出队，计入分配。
			t.queued = t.queued[1:]
			head.status = StatusRunning
			t.running = append(t.running, head)
			t.alloc.CPU += head.demand.CPU
			t.alloc.Mem += head.demand.Mem
			placed = true
			break // 份额已变化，重新排序再选
		}
		if !placed {
			return
		}
	}
}

// lessTenant 定义租户间的 DRF 选择次序：加权主导份额升序，相同则 ID 字典序。
func lessTenant(a, b *tenant, cap Resources) bool {
	sa := weightedShare(a, cap)
	sb := weightedShare(b, cap)
	if c := sa.Cmp(sb); c != 0 {
		return c < 0
	}
	return a.id < b.id
}

// dominantShare 返回租户的主导份额（max(cpu份额, 内存份额)）及其主导资源。
func dominantShare(t *tenant, cap Resources) (*big.Rat, string) {
	cpu := big.NewRat(t.alloc.CPU, cap.CPU)
	mem := big.NewRat(t.alloc.Mem, cap.Mem)
	switch cpu.Cmp(mem) {
	case 1:
		return cpu, "cpu"
	case -1:
		return mem, "mem"
	default:
		return cpu, "cpu" // 份额相等时记为 cpu（仅展示用）
	}
}

// weightedShare 返回 主导份额/权重。全程使用 big.Rat，
// 保证 1/3、0.1 这类权重的平局判定确定且精确，不依赖浮点。
func weightedShare(t *tenant, cap Resources) *big.Rat {
	d, _ := dominantShare(t, cap)
	return new(big.Rat).Quo(d, t.weight)
}

func removeTask(list []*task, id string) []*task {
	for i, tk := range list {
		if tk.id == id {
			return append(list[:i], list[i+1:]...)
		}
	}
	return list
}

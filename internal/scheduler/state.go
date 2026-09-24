package scheduler

import (
	"fmt"
	"math/big"
	"sort"
)

// Snapshot 返回整个调度器的深拷贝快照，可在锁外安全读取。
// 租户按 ID 字典序排列；任务按提交顺序排列（运行队列为放置顺序）。
func (s *Scheduler) Snapshot() StateView {
	s.mu.Lock()
	defer s.mu.Unlock()

	v := StateView{
		Capacity: s.capacity,
		Tenants:  make([]TenantView, 0, len(s.tenants)),
	}

	// 先算出每个排队队首的阻塞原因，供视图标注。
	for _, id := range s.order {
		t := s.tenants[id]
		tv := TenantView{
			ID:            t.id,
			Weight:        t.weight.RatString(),
			WeightDecimal: ratDecimal(t.weight),
			Quota:         t.quota,
			Allocated:     t.alloc,
			NumRunning:    len(t.running),
			NumQueued:     len(t.queued),
			Running:       make([]TaskView, 0, len(t.running)),
			Queued:        make([]TaskView, 0, len(t.queued)),
		}
		d, res := dominantShare(t, s.capacity)
		tv.DominantResource = res
		tv.DominantShare = d.RatString()
		ws := weightedShare(t, s.capacity)
		tv.WeightedShare = ws.RatString()
		tv.WeightedDecimal = ratDecimal(ws)

		for _, tk := range t.running {
			tv.Running = append(tv.Running, s.taskViewLocked(t, tk, ""))
		}
		for i, tk := range t.queued {
			reason := ReasonWaitingInQueue
			if i == 0 {
				// 队首：给出真实阻塞原因（集群 CPU/内存不足或租户配额受限）。
				reason = s.fitLocked(t, tk)
			}
			tv.Queued = append(tv.Queued, s.taskViewLocked(t, tk, reason))
		}
		v.Tenants = append(v.Tenants, tv)
		v.NumRunning += len(t.running)
		v.NumQueued += len(t.queued)
	}
	sort.Slice(v.Tenants, func(i, j int) bool { return v.Tenants[i].ID < v.Tenants[j].ID })

	v.Used = Resources{CPU: totalUsed(s, 0), Mem: totalUsed(s, 1)}
	v.Free = Resources{CPU: s.capacity.CPU - v.Used.CPU, Mem: s.capacity.Mem - v.Used.Mem}
	return v
}

// TaskSnapshot 查询单个任务；第二返回值为 false 表示不存在。
func (s *Scheduler) TaskSnapshot(id string) (TaskView, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	tk, ok := s.tasks[id]
	if !ok {
		return TaskView{}, false
	}
	t := s.tenants[tk.tenant]
	reason := ""
	if tk.status == StatusQueued {
		for i, q := range t.queued {
			if q.id == id {
				if i == 0 {
					reason = s.fitLocked(t, tk)
				} else {
					reason = ReasonWaitingInQueue
				}
				break
			}
		}
	}
	return s.taskViewLocked(t, tk, reason), true
}

func (s *Scheduler) taskViewLocked(t *tenant, tk *task, blockedReason string) TaskView {
	d := dominantOfTask(tk.demand, s.capacity)
	return TaskView{
		ID:            tk.id,
		Tenant:        t.id,
		Status:        tk.status,
		CPU:           tk.demand.CPU,
		Mem:           tk.demand.Mem,
		SubmittedSeq:  tk.seq,
		DominantShare: d.RatString(),
		BlockedReason: blockedReason,
	}
}

// dominantOfTask 返回单个任务的主导份额 max(cpu/cap, mem/cap)。
func dominantOfTask(d Resources, cap Resources) *big.Rat {
	cpu := big.NewRat(d.CPU, cap.CPU)
	mem := big.NewRat(d.Mem, cap.Mem)
	if cpu.Cmp(mem) >= 0 {
		return cpu
	}
	return mem
}

// ratDecimal 将分数格式化为保留 6 位小数的十进制字符串（仅展示用；
// 调度比较始终使用精确的 big.Rat，不使用该值）。
func ratDecimal(r *big.Rat) string {
	f, _ := r.Float64()
	return fmt.Sprintf("%.6f", f)
}

// Package scenario provides built-in workloads that demonstrate priority
// inheritance phenomena: classical priority inversion (with and without PIP),
// three-level transitive inheritance, multi-lock release ordering, and an
// AB-BA deadlock.
package scenario

import (
	"pilab/scheduler"
)

// ID identifies a built-in scenario.
type ID string

const (
	// InversionPIP / InversionNone are the same classical 3-task workload
	// (Mars Pathfinder style) run with and without priority inheritance.
	InversionPIP  ID = "inversion-pip"
	InversionNone ID = "inversion-none"
	// ChainInherit shows priority propagating through two owners (T3 -> T2 -> T1).
	ChainInherit ID = "three-level-inheritance"
	// MultiLockOrder exercises multiple waiters on two locks and LIFO nested
	// release / auto-release on exit.
	MultiLockOrder ID = "multi-lock-order"
	// DeadlockABBA is the classic two-lock circular wait counter-example.
	DeadlockABBA ID = "deadlock-abba"
)

// Scenario is a named, runnable configuration.
type Scenario struct {
	ID          ID               `json:"id"`
	Title       string           `json:"title"`
	Description string           `json:"description"`
	Config      scheduler.Config `json:"config"`
}

// All built-in scenarios, in display order.
func All() []Scenario {
	return []Scenario{
		{
			ID:          InversionPIP,
			Title:       "经典优先级反转（启用 PIP）",
			Description: "L 持有 A；H 需要 A 而阻塞，L 继承 H 的优先级先于 M 运行，快速退出临界区。",
			Config:      inversion(scheduler.InheritancePIP),
		},
		{
			ID:          InversionNone,
			Title:       "经典优先级反转（无继承，对照）",
			Description: "M 抢占持锁的 L，导致高优先级 H 被中等优先级 M 间接延迟——无界优先级反转。",
			Config:      inversion(scheduler.InheritanceNone),
		},
		{
			ID:          ChainInherit,
			Title:       "三级（嵌套）传递优先级继承",
			Description: "T2 持有 B；T1 持有 A 后等待 B；T3 等待 A。T3(9) 的优先级经 T1 传递给 T2：先 1→5，再 5→9。",
			Config:      chain(),
		},
		{
			ID:          MultiLockOrder,
			Title:       "多锁与释放顺序",
			Description: "两把锁、嵌套获取；验证释放顺序（显式解锁 vs 退出时 LIFO 自动释放）与唤醒交接。",
			Config:      multiLock(),
		},
		{
			ID:          DeadlockABBA,
			Title:       "死锁反例：AB-BA 环形等待",
			Description: "P1 持有 A 等 B，P2 持有 B 等 A；等待图成环，调度器检测并报告死锁。",
			Config:      deadlock(),
		},
	}
}

// Get returns a built-in scenario by id.
func Get(id ID) (Scenario, bool) {
	for _, sc := range All() {
		if sc.ID == id {
			return sc, true
		}
	}
	return Scenario{}, false
}

// Run executes a scenario and returns the report.
func Run(sc Scenario) (*scheduler.Report, error) {
	s, err := scheduler.New(sc.Config)
	if err != nil {
		return nil, err
	}
	return s.Run(), nil
}

// inversion builds the classical workload. Priorities: L=1, M=5, H=10.
//
//	T0: L  cpu1, lockA, cpu4, unlockA, cpu1
//	T2: M  cpu2, cpu6 (arrives 2, pure CPU)
//	T3: H  cpu1, lockA, cpu1, unlockA (arrives 3)
func inversion(mode scheduler.InheritMode) scheduler.Config {
	return scheduler.Config{
		Inheritance: mode,
		QueuePolicy: scheduler.QueueFIFO,
		Locks:       []scheduler.LockSpec{{ID: "A"}},
		Tasks: []scheduler.TaskSpec{
			{ID: "L", Base: 1, Arrival: 0, Program: scheduler.Program{
				{Op: scheduler.OpCPU, Ticks: 1},
				{Op: scheduler.OpLock, Lock: "A"},
				{Op: scheduler.OpCPU, Ticks: 4},
				{Op: scheduler.OpUnlock, Lock: "A"},
				{Op: scheduler.OpCPU, Ticks: 1},
			}},
			{ID: "M", Base: 5, Arrival: 2, Program: scheduler.Program{
				{Op: scheduler.OpCPU, Ticks: 6},
			}},
			{ID: "H", Base: 10, Arrival: 3, Program: scheduler.Program{
				{Op: scheduler.OpCPU, Ticks: 1},
				{Op: scheduler.OpLock, Lock: "A"},
				{Op: scheduler.OpCPU, Ticks: 1},
				{Op: scheduler.OpUnlock, Lock: "A"},
			}},
		},
	}
}

// chain builds a three-level *acyclic* transitive inheritance workload.
//
//	T2 (prio 1, arrives 0): lockB, cpu6, unlockB               — owns B
//	T1 (prio 5, arrives 1): cpu1, lockA, lockB, cpu1, unlockB, unlockA
//	                                                       — owns A, waits B
//	T3 (prio 9, arrives 3): cpu1, lockA, cpu1, unlockA        — waits A
//
// The donation chain is T3 -> T1 -> T2: T1 first inherits 5 (donor T1->T2),
// then T2 transitively inherits 9 once T3 joins the chain behind T1.
func chain() scheduler.Config {
	return scheduler.Config{
		Inheritance: scheduler.InheritancePIP,
		QueuePolicy: scheduler.QueueFIFO,
		Locks:       []scheduler.LockSpec{{ID: "A"}, {ID: "B"}},
		Tasks: []scheduler.TaskSpec{
			{ID: "T2", Base: 1, Arrival: 0, Program: scheduler.Program{
				{Op: scheduler.OpLock, Lock: "B"},
				{Op: scheduler.OpCPU, Ticks: 6},
				{Op: scheduler.OpUnlock, Lock: "B"},
			}},
			{ID: "T1", Base: 5, Arrival: 1, Program: scheduler.Program{
				{Op: scheduler.OpCPU, Ticks: 1},
				{Op: scheduler.OpLock, Lock: "A"},
				{Op: scheduler.OpLock, Lock: "B"},
				{Op: scheduler.OpCPU, Ticks: 1},
				{Op: scheduler.OpUnlock, Lock: "B"},
				{Op: scheduler.OpUnlock, Lock: "A"},
			}},
			{ID: "T3", Base: 9, Arrival: 3, Program: scheduler.Program{
				{Op: scheduler.OpCPU, Ticks: 1},
				{Op: scheduler.OpLock, Lock: "A"},
				{Op: scheduler.OpCPU, Ticks: 1},
				{Op: scheduler.OpUnlock, Lock: "A"},
			}},
		},
	}
}

// multiLock exercises two locks, nested acquisition, several waiters, and the
// difference between explicit unlock and LIFO auto-release on exit.
//
//	L (prio 1): lockA, lockB, cpu5, unlockB, cpu1, unlockA
//	M (prio 4): cpu2, lockB, cpu2, unlockB      (arrives 2)
//	H (prio 8): cpu4, lockA, cpu1, unlockA       (arrives 4)
func multiLock() scheduler.Config {
	return scheduler.Config{
		Inheritance: scheduler.InheritancePIP,
		QueuePolicy: scheduler.QueueFIFO,
		Locks:       []scheduler.LockSpec{{ID: "A"}, {ID: "B"}},
		Tasks: []scheduler.TaskSpec{
			{ID: "L", Base: 1, Arrival: 0, Program: scheduler.Program{
				{Op: scheduler.OpLock, Lock: "A"},
				{Op: scheduler.OpLock, Lock: "B"},
				{Op: scheduler.OpCPU, Ticks: 5},
				{Op: scheduler.OpUnlock, Lock: "B"},
				{Op: scheduler.OpCPU, Ticks: 1},
				{Op: scheduler.OpUnlock, Lock: "A"},
			}},
			{ID: "M", Base: 4, Arrival: 2, Program: scheduler.Program{
				{Op: scheduler.OpCPU, Ticks: 2},
				{Op: scheduler.OpLock, Lock: "B"},
				{Op: scheduler.OpCPU, Ticks: 2},
				{Op: scheduler.OpUnlock, Lock: "B"},
			}},
			{ID: "H", Base: 8, Arrival: 4, Program: scheduler.Program{
				{Op: scheduler.OpCPU, Ticks: 4},
				{Op: scheduler.OpLock, Lock: "A"},
				{Op: scheduler.OpCPU, Ticks: 1},
				{Op: scheduler.OpUnlock, Lock: "A"},
			}},
		},
	}
}

// deadlock is the AB-BA circular wait, constructed in the classic way: the
// low-priority task acquires A before the high-priority task even exists; the
// high-priority task then arrives, preempts, and acquires B; they finally
// request each other's lock:
//
//	P1 (prio 3, arrives 0): cpu1, lockA, cpu3, lockB, ...  holds A, wants B at 5
//	P2 (prio 6, arrives 2): cpu1, lockB, cpu1, lockA, ...  holds B, wants A at 5
//
// At t=5 P1 waits on B (held by P2) and P2 waits on A (held by P1): the
// wait-for graph P1 -> P2 -> P1 is a cycle. Priority inheritance cannot break
// this: both tasks are blocked, neither runs to release its lock.
func deadlock() scheduler.Config {
	return scheduler.Config{
		Inheritance: scheduler.InheritancePIP,
		QueuePolicy: scheduler.QueueFIFO,
		Locks:       []scheduler.LockSpec{{ID: "A"}, {ID: "B"}},
		Tasks: []scheduler.TaskSpec{
			{ID: "P1", Base: 3, Arrival: 0, Program: scheduler.Program{
				{Op: scheduler.OpCPU, Ticks: 1},
				{Op: scheduler.OpLock, Lock: "A"},
				{Op: scheduler.OpCPU, Ticks: 3},
				{Op: scheduler.OpLock, Lock: "B"},
				{Op: scheduler.OpCPU, Ticks: 2},
				{Op: scheduler.OpUnlock, Lock: "B"},
				{Op: scheduler.OpUnlock, Lock: "A"},
			}},
			{ID: "P2", Base: 6, Arrival: 2, Program: scheduler.Program{
				{Op: scheduler.OpCPU, Ticks: 1},
				{Op: scheduler.OpLock, Lock: "B"},
				{Op: scheduler.OpCPU, Ticks: 1},
				{Op: scheduler.OpLock, Lock: "A"},
				{Op: scheduler.OpCPU, Ticks: 2},
				{Op: scheduler.OpUnlock, Lock: "A"},
				{Op: scheduler.OpUnlock, Lock: "B"},
			}},
		},
	}
}

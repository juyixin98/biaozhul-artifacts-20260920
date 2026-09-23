package agingqueue

import (
	"container/heap"
	"time"
)

// readyHeap 是就绪作业的派发堆：
// 有效优先级高的在前；有效优先级相同则首次入队序号小的在前（FIFO）。
// FIFO 比较使用作业整个生命周期不变的 seq，因此重试换不来新的队首位置。
type readyHeap []*Job

func (h readyHeap) Len() int { return len(h) }

func (h readyHeap) Less(i, j int) bool {
	a, b := h[i], h[j]
	if a.curPriority != b.curPriority {
		return a.curPriority > b.curPriority
	}
	return a.seq < b.seq
}

func (h readyHeap) Swap(i, j int) {
	h[i], h[j] = h[j], h[i]
	h[i].heapIdx = i
	h[j].heapIdx = j
}

func (h *readyHeap) Push(x any) {
	j := x.(*Job)
	j.heapIdx = len(*h)
	*h = append(*h, j)
}

func (h *readyHeap) Pop() any {
	old := *h
	n := len(old)
	x := old[n-1]
	old[n-1] = nil
	x.heapIdx = -1
	*h = old[:n-1]
	return x
}

// delayedEntry 把退避中的作业与其回到就绪队列的时刻绑定。
type delayedEntry struct {
	readyAt time.Time
	job     *Job
	idx     int
}

type delayedHeap []*delayedEntry

func (h delayedHeap) Len() int { return len(h) }

func (h delayedHeap) Less(i, j int) bool {
	if !h[i].readyAt.Equal(h[j].readyAt) {
		return h[i].readyAt.Before(h[j].readyAt)
	}
	// 同一时刻到期，仍按首次入队序号稳定排序。
	return h[i].job.seq < h[j].job.seq
}

func (h delayedHeap) Swap(i, j int) {
	h[i], h[j] = h[j], h[i]
	h[i].idx = i
	h[j].idx = j
}

func (h *delayedHeap) Push(x any) {
	e := x.(*delayedEntry)
	e.idx = len(*h)
	*h = append(*h, e)
}

func (h *delayedHeap) Pop() any {
	old := *h
	n := len(old)
	x := old[n-1]
	old[n-1] = nil
	x.idx = -1
	*h = old[:n-1]
	return x
}

// boostEntry 记录某个就绪作业下一次"等待老化跳一档"的时刻。
// 一个作业在就绪堆里任意时刻最多只有一条 boost 记录；跳档后再排下一条。
type boostEntry struct {
	at  time.Time
	job *Job
	idx int
}

type boostHeap []*boostEntry

func (h boostHeap) Len() int { return len(h) }

func (h boostHeap) Less(i, j int) bool {
	if !h[i].at.Equal(h[j].at) {
		return h[i].at.Before(h[j].at)
	}
	return h[i].job.seq < h[j].job.seq
}

func (h boostHeap) Swap(i, j int) {
	h[i], h[j] = h[j], h[i]
	h[i].idx = i
	h[j].idx = j
}

func (h *boostHeap) Push(x any) {
	e := x.(*boostEntry)
	e.idx = len(*h)
	*h = append(*h, e)
}

func (h *boostHeap) Pop() any {
	old := *h
	n := len(old)
	x := old[n-1]
	old[n-1] = nil
	x.idx = -1
	*h = old[:n-1]
	return x
}

// 静态断言：三个堆均满足 heap.Interface。
var (
	_ heap.Interface = (*readyHeap)(nil)
	_ heap.Interface = (*delayedHeap)(nil)
	_ heap.Interface = (*boostHeap)(nil)
)

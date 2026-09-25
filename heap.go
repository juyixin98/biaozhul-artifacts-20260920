package deadlineadm

import "container/heap"

// edfHeap orders queued jobs earliest-deadline-first, breaking ties by
// submission sequence (FIFO).
type edfHeap []*Job

func (h edfHeap) Len() int { return len(h) }

func (h edfHeap) Less(i, j int) bool {
	if !h[i].deadline.Equal(h[j].deadline) {
		return h[i].deadline.Before(h[j].deadline)
	}
	return h[i].seq < h[j].seq
}

func (h edfHeap) Swap(i, j int) {
	h[i], h[j] = h[j], h[i]
	h[i].heapIndex = i
	h[j].heapIndex = j
}

func (h *edfHeap) Push(x any) {
	j := x.(*Job)
	j.heapIndex = len(*h)
	*h = append(*h, j)
}

func (h *edfHeap) Pop() any {
	old := *h
	n := len(old)
	j := old[n-1]
	old[n-1] = nil
	j.heapIndex = -1
	*h = old[:n-1]
	return j
}

// removeAt deletes the job at index i (used when a queued job is canceled).
func (h *edfHeap) removeAt(i int) *Job {
	return heap.Remove(h, i).(*Job)
}

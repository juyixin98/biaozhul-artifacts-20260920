package dynpool

// Status returns a consistent snapshot of pool metrics.
func (p *Pool) Status() Status {
	p.mu.Lock()
	s := Status{
		Name:          p.name,
		State:         p.state.String(),
		TargetWorkers: p.target,
		Retiring:      p.retiring,
		Queued:        len(p.queue),
		QueueCap:      cap(p.queue),
	}
	p.mu.Unlock()

	s.ActiveWorkers = int(p.active.Load())
	s.RunningTasks = int(p.running.Load())
	s.CompletedTasks = p.completed.Load()
	s.RejectedTasks = p.rejectedN.Load()
	s.CancelledTasks = p.canceledN.Load()
	s.DroppedTasks = p.droppedN.Load()
	return s
}

// Handle returns the handle of a previously submitted task. It remains
// available after the pool stops.
func (p *Pool) Handle(id string) (*Handle, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	h, ok := p.handles[id]
	return h, ok
}

// TaskIDs returns accepted task ids in submission order (ids are allocated
// under the pool lock; the slice is a snapshot).
func (p *Pool) TaskIDs() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	ids := make([]string, 0, len(p.handles))
	for id := range p.handles {
		ids = append(ids, id)
	}
	return ids
}

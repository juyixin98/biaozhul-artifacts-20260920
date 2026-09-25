package scheduler

import "sync"

// deque is a per-worker local task queue with work-stealing access:
// the owning worker pushes and pops at the bottom (LIFO, hot tasks
// first), while thieves pop from the top (FIFO). It is a simplified,
// mutex-protected Chase-Lev deque: the lock makes the correctness
// argument straightforward while still allowing independent progress
// between local operations and steals.
//
// The backing slice grows on demand so a burst of spawned tasks is
// never rejected. Slots above size are nil so references to popped
// tasks are released.
type deque struct {
	mu sync.Mutex
	// buf is a circular buffer. top is the index of the oldest task;
	// (top+size)%cap is the next push position.
	top  int
	size int
	buf  []*task
}

func newDeque(initialCap int) *deque {
	if initialCap < 2 {
		initialCap = 16
	}
	return &deque{buf: make([]*task, initialCap)}
}

// pushBottom adds t for the owning worker.
func (d *deque) pushBottom(t *task) {
	d.mu.Lock()
	if d.size == len(d.buf) {
		d.growLocked()
	}
	d.buf[(d.top+d.size)%len(d.buf)] = t
	d.size++
	d.mu.Unlock()
}

// popBottom removes and returns the newest task for the owning worker.
// Returns nil when empty.
func (d *deque) popBottom() *task {
	d.mu.Lock()
	if d.size == 0 {
		d.mu.Unlock()
		return nil
	}
	d.size--
	idx := (d.top + d.size) % len(d.buf)
	t := d.buf[idx]
	d.buf[idx] = nil
	d.mu.Unlock()
	return t
}

// popTop removes and returns the oldest task for a thief. Returns nil
// when empty.
func (d *deque) popTop() *task {
	d.mu.Lock()
	if d.size == 0 {
		d.mu.Unlock()
		return nil
	}
	t := d.buf[d.top]
	d.buf[d.top] = nil
	d.top = (d.top + 1) % len(d.buf)
	d.size--
	d.mu.Unlock()
	return t
}

// stealUpTo moves up to max tasks (oldest first) out of d into out.
// Stealing half the deque is the classic policy: callers pass
// max=(size+1)/2 computed against a pre-read length, but the final
// cap is re-checked here under the lock. It returns the number moved.
func (d *deque) stealUpTo(max int, out []*task) int {
	d.mu.Lock()
	n := d.size
	if max < n {
		n = max
	}
	if n <= 0 {
		d.mu.Unlock()
		return 0
	}
	for i := 0; i < n; i++ {
		idx := (d.top + i) % len(d.buf)
		out[i] = d.buf[idx]
		d.buf[idx] = nil
	}
	d.top = (d.top + n) % len(d.buf)
	d.size -= n
	d.mu.Unlock()
	return n
}

// len reports the number of queued tasks.
func (d *deque) len() int {
	d.mu.Lock()
	n := d.size
	d.mu.Unlock()
	return n
}

// growLocked doubles the backing buffer. Callers hold d.mu and the
// buffer is guaranteed full.
func (d *deque) growLocked() {
	old := d.buf
	bigger := make([]*task, len(old)*2)
	for i := 0; i < d.size; i++ {
		bigger[i] = old[(d.top+i)%len(old)]
	}
	d.buf = bigger
	d.top = 0
}

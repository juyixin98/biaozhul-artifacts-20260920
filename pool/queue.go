package pool

// ring is a fixed-capacity FIFO of work items.
type workItem struct {
	task Task
	fut  *Future
}

type ring struct {
	buf  []workItem
	head int
	tail int
	n    int
}

func newRing(capacity int) *ring {
	return &ring{buf: make([]workItem, capacity)}
}

func (r *ring) len() int   { return r.n }
func (r *ring) full() bool { return r.n == len(r.buf) }

// push appends an item; caller must ensure !full.
func (r *ring) push(it workItem) {
	r.buf[r.tail] = it
	r.tail++
	if r.tail == len(r.buf) {
		r.tail = 0
	}
	r.n++
}

// pop removes and returns the oldest item; ok is false when empty.
func (r *ring) pop() (it workItem, ok bool) {
	if r.n == 0 {
		return workItem{}, false
	}
	it = r.buf[r.head]
	var zero workItem
	r.buf[r.head] = zero // avoid retaining task closures
	r.head++
	if r.head == len(r.buf) {
		r.head = 0
	}
	r.n--
	return it, true
}

// drain removes all items in FIFO order.
func (r *ring) drain() []workItem {
	out := make([]workItem, 0, r.n)
	for {
		it, ok := r.pop()
		if !ok {
			return out
		}
		out = append(out, it)
	}
}

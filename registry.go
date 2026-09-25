package trmerge

import "sync"

// runnerRegistry tracks currently running execute-mode runners by run id.
type runnerRegistry struct {
	mu sync.Mutex
	m  map[string]*Runner
}

var active = runnerRegistry{m: map[string]*Runner{}}

func (r *runnerRegistry) Store(id string, rnr *Runner) {
	r.mu.Lock()
	r.m[id] = rnr
	r.mu.Unlock()
}

func (r *runnerRegistry) Get(id string) *Runner {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.m[id]
}

func (r *runnerRegistry) Delete(id string) {
	r.mu.Lock()
	delete(r.m, id)
	r.mu.Unlock()
}

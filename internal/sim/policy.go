package sim

// splitmix64 is a self-contained, deterministic pseudo-random generator.
// Using it (instead of math/rand/v2's runtime-seeded helpers) guarantees that
// the same seed produces the same draws on every machine and Go version.
type rng struct {
	state uint64
}

func newRNG(seed uint64) *rng { return &rng{state: seed} }

func (r *rng) next() uint64 {
	r.state += 0x9E3779B97F4A7C15
	z := r.state
	z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9
	z = (z ^ (z >> 27)) * 0x94D049BB133111EB
	return z ^ (z >> 31)
}

func (r *rng) float64() float64 {
	return float64(r.next()>>11) / float64(1<<53)
}

func (r *rng) intn(n int) int {
	if n <= 0 {
		return 0
	}
	return int(r.next() % uint64(n))
}

// policy decides the fate of one transmission: how many copies to deliver
// (0 = dropped) and the delay of each copy.
type policy interface {
	// decide returns (copies delivered, per-copy delays).
	decide(m message, r *rng) (int, []int)
}

type stochasticPolicy struct {
	loss, dup     float64
	minD, maxD    int
	reorderWindow int
}

func (p *stochasticPolicy) delay(r *rng) int {
	if p.maxD <= p.minD {
		return p.minD
	}
	d := p.minD + r.intn(p.maxD-p.minD+1)
	if p.reorderWindow > 0 {
		// Reorder: push a fraction of messages forward/backward inside the
		// window so they overtake in-flight neighbours.
		if r.float64() < 0.35 {
			d += r.intn(2*p.reorderWindow+1) - p.reorderWindow
			if d < 0 {
				d = 0
			}
		}
	}
	return d
}

func (p *stochasticPolicy) decide(m message, r *rng) (int, []int) {
	if r.float64() < p.loss {
		return 0, nil
	}
	copies := 1
	if p.dup > 0 && r.float64() < p.dup {
		copies = 2
	}
	delays := make([]int, copies)
	for i := range delays {
		delays[i] = p.delay(r)
	}
	return copies, delays
}

type scriptedPolicy struct {
	rules []Rule
}

func (p *scriptedPolicy) decide(m message, r *rng) (int, []int) {
	for i := range p.rules {
		ru := &p.rules[i]
		if !ruleMatches(ru, m) {
			continue
		}
		delay := ru.Delay
		if delay <= 0 {
			delay = 1
		}
		switch ru.Action {
		case "drop":
			return 0, nil
		case "duplicate":
			n := ru.Duplicates
			if n < 1 {
				n = 1
			}
			ds := make([]int, n+1)
			for j := range ds {
				ds[j] = delay
			}
			return n + 1, ds
		}
		return 1, []int{delay}
	}
	// Default for scripted mode: deliver in one tick.
	return 1, []int{1}
}

func ruleMatches(ru *Rule, m message) bool {
	if ru.From != "" && ru.From != m.from {
		return false
	}
	if ru.To != "" && ru.To != m.to {
		return false
	}
	if ru.Role != "" && ru.Role != m.role {
		return false
	}
	if ru.MsgType != "" && ru.MsgType != m.msgType {
		return false
	}
	if ru.Op != "" && ru.Op != m.opKind {
		return false
	}
	if ru.MinAttempt > 0 && m.attempt < ru.MinAttempt {
		return false
	}
	return true
}

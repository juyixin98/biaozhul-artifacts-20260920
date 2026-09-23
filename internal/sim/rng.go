package sim

// Rng is a tiny deterministic PCG-style generator so that scenario results are
// reproducible across machines and Go versions (math/rand's stream has changed
// between releases). Only the few methods the simulator needs are provided.
type Rng struct {
	state uint64
	inc   uint64
}

// NewRng seeds a generator. The stream choice follows the PCG reference
// initialization so different seeds produce well-separated sequences.
func NewRng(seed int64) *Rng {
	r := &Rng{state: 0, inc: 1442695040888963407}
	r.Uint32()
	r.state += uint64(seed)
	r.Uint32()
	return r
}

// Uint32 returns the next pseudo-random uint32.
func (r *Rng) Uint32() uint32 {
	oldstate := r.state
	r.state = oldstate*6364136223846793005 + r.inc
	xorshifted := uint32(((oldstate >> 18) ^ oldstate) >> 27)
	rot := uint32(oldstate >> 59)
	return (xorshifted >> rot) | (xorshifted << ((-rot) & 31))
}

// Intn returns a uniform value in [0, n). n must be > 0.
func (r *Rng) Intn(n int) int {
	if n <= 0 {
		panic("sim: Rng.Intn with non-positive bound")
	}
	return int(r.Uint32()) % n
}

// Chance reports true with probability p (p is clamped to [0,1]).
func (r *Rng) Chance(p float64) bool {
	if p <= 0 {
		return false
	}
	if p >= 1 {
		return true
	}
	return float64(r.Uint32())/(1<<32) < p
}

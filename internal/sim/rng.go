package sim

// Rng is a tiny deterministic PRNG (Numerical Recipes / L'Ecuyer style
// constants) so that probabilistic network faults depend only on the
// scenario seed and never on Go's global, process-dependent RNG.
type Rng struct {
	state uint64
}

func NewRng(seed uint64) *Rng {
	r := &Rng{state: seed}
	// Warm up so that seed 0 is not degenerate.
	for i := 0; i < 16; i++ {
		r.Uint64()
	}
	return r
}

// Uint64 returns the next pseudo-random 64-bit value.
func (r *Rng) Uint64() uint64 {
	// mmix constants by Donald Knuth.
	r.state = r.state*6364136223846793005 + 1442695040888963407
	return r.state
}

// Float64 returns a value in [0,1).
func (r *Rng) Float64() float64 {
	return float64(r.Uint64()>>11) / float64(1<<53)
}

// Bernoulli returns true with probability p.
func (r *Rng) Bernoulli(p float64) bool {
	if p <= 0 {
		return false
	}
	if p >= 1 {
		return true
	}
	return r.Float64() < p
}

// Intn returns a uniform integer in [0,n). n must be positive.
func (r *Rng) Intn(n int) int {
	if n <= 0 {
		panic("Intn with non-positive n")
	}
	return int(r.Uint64() % uint64(n))
}

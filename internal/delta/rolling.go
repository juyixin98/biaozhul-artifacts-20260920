package delta

// mod is the rsync 16-bit rolling checksum modulus.
const mod = 1 << 16

// roller is an rsync-style Adler-derived rolling checksum over a fixed-size
// window. It supports pushing a byte (window fill) and rolling the window
// forward (drop oldest, add newest).
//
// All arithmetic is performed modulo 2^16 for a and modulo 2^16 for b, matching
// get_checksum1 in rsync, which keeps the values small and collision-prone —
// the strong SHA-256 checksum is the authority; the weak checksum only indexes
// candidate blocks.
type roller struct {
	a, b uint32
	n    int
}

func (r *roller) add(x byte) {
	r.a = (r.a + uint32(x)) % mod
	r.b = (r.b + r.a) % mod
	r.n++
}

// roll advances the window: remove outgoing byte x (oldest), add incoming
// byte y (newest). Requires a full window.
func (r *roller) roll(x, y byte) {
	// yA = (a - x + y) mod M
	// yB = (b - n*x + yA) mod M
	r.a = (r.a + uint32(y) - uint32(x) + mod) % mod
	v := int64(r.b) - int64(r.n)*int64(x) + int64(r.a)
	v %= mod
	if v < 0 {
		v += mod
	}
	r.b = uint32(v)
}

func (r *roller) sum() uint32 { return r.a + (r.b << 16) }

// weakNaive is the reference implementation used by tests: sum of bytes and
// weighted positions over an arbitrary slice.
func weakNaive(p []byte) uint32 {
	var a, b uint32
	n := uint32(len(p))
	for i, x := range p {
		a = (a + uint32(x)) % mod
		b = (b + (n-uint32(i))*uint32(x)) % mod
	}
	return a + (b << 16)
}

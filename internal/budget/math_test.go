package budget

import (
	"math"
	"testing"
)

func TestDiv128By64Small(t *testing.T) {
	// 100 / 7 = 14 rem 2.
	qhi, qlo, rem := div128by64(0, 100, 7)
	if qhi != 0 || qlo != 14 || rem != 2 {
		t.Fatalf("100/7 = (%d:%d) rem %d, want 14 rem 2", qhi, qlo, rem)
	}
}

func TestDiv128By6464BitQuotient(t *testing.T) {
	// 2^64 / 3 = 6148914691236517205 rem 1 (quotient fits in 64 bits).
	qhi, qlo, rem := div128by64(1, 0, 3)
	if qhi != 0 || qlo != uint64(6148914691236517205) || rem != 1 {
		t.Fatalf("2^64/3 = (%d:%d) rem %d", qhi, qlo, rem)
	}
}

func TestDiv128By64WideQuotient(t *testing.T) {
	// (2^128 - 1) / 1.
	all := uint64(math.MaxUint64)
	qhi, qlo, rem := div128by64(all, all, 1)
	if qhi != all || qlo != all || rem != 0 {
		t.Fatalf("2^128-1 /1 = (%d:%d) rem %d", qhi, qlo, rem)
	}
	// (2^128 - 1) / (2^64-1) = 2^64 + 1 ... verify (q*d + r == n) via limbs.
	qhi, qlo, rem = div128by64(all, all, all)
	// q = 0x1_00000000_00000001 (2^64+1), r=0.
	if qhi != 1 || qlo != 1 || rem != 0 {
		t.Fatalf("(2^128-1)/(2^64-1) = (%d:%d) rem %d, want 1:1 rem 0", qhi, qlo, rem)
	}
}

func TestDivCeil(t *testing.T) {
	q, r := div128by64Ceil(0, 10, 3)
	if q.hi != 0 || q.lo != 4 || r != 1 {
		t.Fatalf("ceil(10/3) = %d rem %d, want 4 rem 1", q.lo, r)
	}
	q, r = div128by64Ceil(0, 9, 3)
	if q.lo != 3 || r != 0 {
		t.Fatalf("ceil(9/3) = %d rem %d, want 3 rem 0", q.lo, r)
	}
}

func TestMul64(t *testing.T) {
	r := mul64(1<<32, 1<<32) // 2^64
	if r.hi != 1 || r.lo != 0 {
		t.Fatalf("2^32*2^32 = %d:%d", r.hi, r.lo)
	}
}

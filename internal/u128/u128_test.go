package u128

import (
	"math/big"
	"math/rand"
	"testing"
)

func toBig(x U128) *big.Int {
	b := new(big.Int).SetUint64(x.Hi)
	b.Lsh(b, 64)
	b.Or(b, new(big.Int).SetUint64(x.Lo))
	return b
}

func fromBig(b *big.Int) U128 {
	mask := new(big.Int).Sub(new(big.Int).Lsh(big.NewInt(1), 64), big.NewInt(1))
	lo := new(big.Int).And(b, mask).Uint64()
	hi := new(big.Int).Rsh(b, 64).Uint64()
	return U128{Hi: hi, Lo: lo}
}

func TestMulAndDivAgainstBigInt(t *testing.T) {
	rng := rand.New(rand.NewSource(1))
	for i := 0; i < 10_000; i++ {
		var a, b, d uint64
		switch {
		case i < 100: // edge values
			edges := []uint64{0, 1, 2, 3, 7, 1 << 32, 1 << 63, 1<<63 - 1, 1<<64 - 2, 1<<64 - 1}
			a = edges[rng.Intn(len(edges))]
			b = edges[rng.Intn(len(edges))]
			d = edges[1+rng.Intn(len(edges)-1)] // never 0
		default:
			a = rng.Uint64()
			b = rng.Uint64()
			d = rng.Uint64()
			if d == 0 {
				d = 1
			}
		}

		pBig := new(big.Int).Mul(new(big.Int).SetUint64(a), new(big.Int).SetUint64(b))
		if pBig.BitLen() > 128 {
			continue // outside this helper's domain
		}
		p := Mul64(a, b)
		if toBig(p).Cmp(pBig) != 0 {
			t.Fatalf("Mul64(%d,%d) = %s want %s", a, b, toBig(p), pBig)
		}

		q, r := DivU64(p, d)
		bq, br := new(big.Int).QuoRem(pBig, new(big.Int).SetUint64(d), new(big.Int))
		if toBig(q).Cmp(bq) != 0 || toBig(r).Cmp(br) != 0 {
			t.Fatalf("DivU64(%s,%d) = %s rem %s; want %s rem %s",
				pBig, d, toBig(q), toBig(r), bq, br)
		}

		// MulU64 check when it stays within 128 bits.
		p2Big := new(big.Int).Mul(toBig(p), new(big.Int).SetUint64(d))
		if p2Big.BitLen() <= 128 {
			p2 := MulU64(p, d)
			if toBig(p2).Cmp(p2Big) != 0 {
				t.Fatalf("MulU64(%s,%d) = %s want %s", toBig(p), d, toBig(p2), p2Big)
			}
		}
	}
}

func TestKnownDiv(t *testing.T) {
	// 2^100 / 3 -> quotient 422550200076076467165567735125, rem 1
	x := U128{Hi: 1 << 36, Lo: 0}
	q, r := DivU64(x, 3)
	if r.Lo != 1 {
		t.Fatalf("rem=%d want 1", r.Lo)
	}
	wantQ, _ := new(big.Int).SetString("422550200076076467165567735125", 10)
	if toBig(q).Cmp(wantQ) != 0 {
		t.Fatalf("q=%s want %s", toBig(q), wantQ)
	}
}

func TestFitsInt64(t *testing.T) {
	if !From64(1 << 62).FitsInt64() {
		t.Fatal("2^62 should fit")
	}
	if From64(1 << 63).FitsInt64() {
		t.Fatal("2^63 should not fit in int64")
	}
	if (U128{Hi: 1}).FitsInt64() {
		t.Fatal("hi!=0 should not fit")
	}
}

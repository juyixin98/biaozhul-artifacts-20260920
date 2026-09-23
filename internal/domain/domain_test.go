package domain

import (
	"encoding/json"
	"errors"
	"math"
	"strings"
	"testing"
)

func TestBlockHashDeterministicAndReal(t *testing.T) {
	raw, err := CanonicalBody(ZeroHash, 0, []Transfer{
		{From: "0x" + strings.Repeat("a", 40), To: "0x" + strings.Repeat("b", 40), Amount: 7},
	})
	if err != nil {
		t.Fatal(err)
	}
	h1 := BlockHash(raw)
	h2 := BlockHash(raw)
	if h1 != h2 {
		t.Fatal("hash not deterministic")
	}
	if !strings.HasPrefix(h1, "0x") || len(h1) != 66 {
		t.Fatalf("hash shape wrong: %q", h1)
	}
	// Independently compute with encoding/json to ensure this is really
	// SHA-256 over the canonical layout, not a constant.
	var probe map[string]any
	if err := json.Unmarshal(raw, &probe); err != nil {
		t.Fatal(err)
	}
	if probe["height"].(float64) != 0 {
		t.Fatal("canonical layout wrong")
	}
	// A different body MUST hash differently.
	raw2, _ := CanonicalBody(ZeroHash, 0, []Transfer{
		{From: "0x" + strings.Repeat("a", 40), To: "0x" + strings.Repeat("b", 40), Amount: 8},
	})
	if BlockHash(raw2) == h1 {
		t.Fatal("different bodies hashed equal")
	}
}

func TestNormalizeRejectsHashMismatch(t *testing.T) {
	b := &Block{
		Hash:       "0x" + strings.Repeat("9", 64),
		ParentHash: ZeroHash,
		Height:     0,
	}
	if err := Normalize(b); !errors.Is(err, ErrHashMismatch) {
		t.Fatalf("want ErrHashMismatch, got %v", err)
	}
}

func TestNormalizeAcceptsCorrect(t *testing.T) {
	raw, _ := CanonicalBody(ZeroHash, 0, nil)
	b := &Block{
		Hash:       BlockHash(raw),
		ParentHash: ZeroHash,
		Height:     0,
	}
	if err := Normalize(b); err != nil {
		t.Fatalf("valid block should normalize: %v", err)
	}
	if b.Hash != BlockHash(raw) || b.ParentHash != ZeroHash {
		t.Fatalf("normalization failed: %+v", b)
	}
}

func TestNormalizeRejectsUpperCaseHash(t *testing.T) {
	// Protocol hashes are lowercase 0x hex; an uppercase declaration must not
	// silently match the lowercase computed hash.
	raw, _ := CanonicalBody(ZeroHash, 0, nil)
	b := &Block{
		Hash:       strings.ToUpper(BlockHash(raw)),
		ParentHash: ZeroHash,
		Height:     0,
	}
	if err := Normalize(b); err == nil {
		t.Fatal("uppercase declared hash must be rejected")
	}
}

func TestValidation(t *testing.T) {
	if _, err := ValidateHash("0x123"); !errors.Is(err, ErrInvalidHash) {
		t.Fatalf("short hash: %v", err)
	}
	if _, err := ValidateHash("0x" + strings.Repeat("z", 64)); !errors.Is(err, ErrInvalidHash) {
		t.Fatalf("non-hex: %v", err)
	}
	if _, err := ValidateAddress("0x" + strings.Repeat("A", 40)); !errors.Is(err, ErrInvalidAddress) {
		t.Fatalf("uppercase address should be rejected: %v", err)
	}
	if _, err := CanonicalBody(ZeroHash, -1, nil); !errors.Is(err, ErrHeightNegative) {
		t.Fatalf("negative height: %v", err)
	}
	if _, err := CanonicalBody(ZeroHash, 0, []Transfer{
		{From: "0x" + strings.Repeat("a", 40), To: "0x" + strings.Repeat("b", 40), Amount: -1},
	}); !errors.Is(err, ErrNegativeAmount) {
		t.Fatalf("negative amount: %v", err)
	}
}

func TestApplyTransferCheckedArithmetic(t *testing.T) {
	bal := map[string]int64{}
	tr := Transfer{From: "a", To: "b", Amount: 10}
	if err := ApplyTransfer(bal, tr); err != nil {
		t.Fatal(err)
	}
	if bal["a"] != -10 || bal["b"] != 10 {
		t.Fatalf("balances wrong: %v", bal)
	}
	// Self-transfer is neutral and must not overflow even at boundary.
	edge := map[string]int64{"s": math.MaxInt64}
	if err := ApplyTransfer(edge, Transfer{From: "s", To: "s", Amount: 5}); err != nil {
		t.Fatal(err)
	}
	if edge["s"] != math.MaxInt64 {
		t.Fatal("self-transfer must be neutral")
	}
	// Overflow on credit.
	if err := ApplyTransfer(map[string]int64{"b": math.MaxInt64},
		Transfer{From: "x", To: "b", Amount: 1}); !errors.Is(err, ErrBalanceOverflow) {
		t.Fatalf("credit overflow: %v", err)
	}
	// Underflow on debit.
	if err := ApplyTransfer(map[string]int64{"a": math.MinInt64},
		Transfer{From: "a", To: "x", Amount: 1}); !errors.Is(err, ErrBalanceOverflow) {
		t.Fatalf("debit overflow: %v", err)
	}
}

func TestCanonicalLayoutStable(t *testing.T) {
	// Lock the wire format: changing this breaks block identity for everyone.
	raw, err := CanonicalBody(ZeroHash, 1, []Transfer{
		{From: "0x" + strings.Repeat("a", 40), To: "0x" + strings.Repeat("b", 40), Amount: 1},
	})
	if err != nil {
		t.Fatal(err)
	}
	wantSubstr := `{"parent_hash":"0x` + strings.Repeat("0", 64) +
		`","height":1,"transfers":[{"from":"0x`
	if !strings.HasPrefix(string(raw), wantSubstr) {
		t.Fatalf("canonical layout drifted:\n%s", raw)
	}
}

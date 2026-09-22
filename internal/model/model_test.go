package model

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
	"testing"
)

func TestCanonicalHashIsDeterministic(t *testing.T) {
	b := Block{
		Height:     7,
		ParentHash: "0x" + strings.Repeat("ab", 32),
		Transactions: []Transfer{
			{From: "0x" + strings.Repeat("a", 40), To: "0x" + strings.Repeat("b", 40), Amount: "42"},
		},
	}
	nb1, pre1, err := Normalize(b)
	if err != nil {
		t.Fatal(err)
	}
	nb2, pre2, err := Normalize(b)
	if err != nil {
		t.Fatal(err)
	}
	if nb1.Hash != nb2.Hash || string(pre1) != string(pre2) {
		t.Fatal("normalization not deterministic")
	}

	// Independently compute SHA-256 over the canonical compact JSON.
	var pre map[string]any
	if err := json.Unmarshal(pre1, &pre); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(pre1)
	want := "0x" + hex.EncodeToString(sum[:])
	if nb1.Hash != want {
		t.Fatalf("hash %s != actual sha256 %s", nb1.Hash, want)
	}

	// Keys must be lexicographically ordered: height, parentHash, transactions.
	s := string(pre1)
	hi := strings.Index(s, `"height"`)
	pi := strings.Index(s, `"parentHash"`)
	ti := strings.Index(s, `"transactions"`)
	if !(hi >= 0 && hi < pi && pi < ti) {
		t.Fatalf("preimage key order wrong: %s", s)
	}
}

func TestHashVerifiesAgainstClaimed(t *testing.T) {
	nb, _, err := Normalize(Block{Height: 0, Transactions: nil})
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := Normalize(Block{Hash: nb.Hash, Height: 0}); err != nil {
		t.Fatalf("correct claimed hash rejected: %v", err)
	}
	bad := "0x" + strings.Repeat("00", 31) + "01"
	if _, _, err := Normalize(Block{Hash: bad, Height: 0}); err == nil {
		t.Fatal("wrong claimed hash accepted")
	}
}

func TestAmountCanonicalization(t *testing.T) {
	for _, in := range []string{"007", "+7", "  7  "} {
		nb, _, err := Normalize(Block{Height: 0, Transactions: []Transfer{
			{From: "", To: "0x" + strings.Repeat("1", 40), Amount: in},
		}})
		if err != nil {
			t.Fatalf("amount %q: %v", in, err)
		}
		if got := nb.Transactions[0].Amount; got != "7" {
			t.Fatalf("amount %q normalized to %q, want 7", in, got)
		}
	}
}

func TestValidationRules(t *testing.T) {
	addr := "0x" + strings.Repeat("a", 40)
	cases := []struct {
		name string
		b    Block
	}{
		{"negative height", Block{Height: -1}},
		{"genesis nonzero parent", Block{Height: 0, ParentHash: "0x" + strings.Repeat("1", 32)}},
		{"non-genesis zero parent", Block{Height: 1, ParentHash: GenesisParent}},
		{"malformed parent hash", Block{Height: 1, ParentHash: "0xnope"}},
		{"malformed from", Block{Height: 0, Transactions: []Transfer{{From: "alice", To: addr, Amount: "1"}}}},
		{"malformed to", Block{Height: 0, Transactions: []Transfer{{From: addr, To: "bob", Amount: "1"}}}},
		{"non-integer amount", Block{Height: 0, Transactions: []Transfer{{From: addr, To: addr, Amount: "1.5"}}}},
		{"negative amount", Block{Height: 0, Transactions: []Transfer{{From: addr, To: addr, Amount: "-3"}}}},
		{"malformed claimed hash", Block{Height: 0, Hash: "xyz"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, _, err := Normalize(tc.b); err == nil {
				t.Fatal("expected validation error")
			}
		})
	}
}

func TestEmptyAddressMintBurnAllowed(t *testing.T) {
	nb, _, err := Normalize(Block{Height: 0, Transactions: []Transfer{
		{From: "", To: "", Amount: "0"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if nb.Transactions[0].From != "" || nb.Transactions[0].To != "" {
		t.Fatal("empty mint/burn addresses not preserved")
	}
}

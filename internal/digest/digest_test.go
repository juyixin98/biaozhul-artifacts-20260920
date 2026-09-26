package digest

import "testing"

func TestOfStableAcrossKeyOrderAndWhitespace(t *testing.T) {
	a := []byte(`{"amount":100,"currency":"USD"}`)
	b := []byte(`{ "currency": "USD", "amount": 100 }`)
	c := []byte("{\n\t\"amount\": 100,\n\t\"currency\": \"USD\"\n}")

	ha, err := Of("POST", "/v1/orders", a)
	if err != nil {
		t.Fatalf("digest a: %v", err)
	}
	hb, err := Of("POST", "/v1/orders", b)
	if err != nil {
		t.Fatalf("digest b: %v", err)
	}
	hc, err := Of("POST", "/v1/orders", c)
	if err != nil {
		t.Fatalf("digest c: %v", err)
	}
	if ha != hb || hb != hc {
		t.Fatalf("canonical hashes differ: %s %s %s", ha, hb, hc)
	}
}

func TestOfDistinguishesPayloads(t *testing.T) {
	h1, _ := Of("POST", "/v1/orders", []byte(`{"amount":100}`))
	h2, _ := Of("POST", "/v1/orders", []byte(`{"amount":101}`))
	if h1 == h2 {
		t.Fatal("different amounts produced the same hash")
	}
	h3, _ := Of("GET", "/v1/orders", []byte(`{"amount":100}`))
	if h1 == h3 {
		t.Fatal("different methods produced the same hash")
	}
}

func TestOfRejectsInvalidJSON(t *testing.T) {
	if _, err := Of("POST", "/v1/orders", []byte("not-json")); err == nil {
		t.Fatal("expected error for invalid JSON")
	}
	if _, err := Of("POST", "/v1/orders", []byte(`{"a":1} {"b":2}`)); err == nil {
		t.Fatal("expected error for two JSON values")
	}
}

func TestOfEmptyBodyIsNull(t *testing.T) {
	h, err := Of("POST", "/v1/orders", nil)
	if err != nil {
		t.Fatalf("empty body: %v", err)
	}
	if len(h) != 64 {
		t.Fatalf("hash length = %d, want 64 hex chars", len(h))
	}
}

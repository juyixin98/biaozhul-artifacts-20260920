package registry

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestStoreHandler(t *testing.T) {
	s := NewStore()
	s.Put(Contract{
		Service:  "orders",
		Version:  "v1",
		Request:  json.RawMessage(`{"type":"object"}`),
		Response: json.RawMessage(`{"type":"object"}`),
	})
	srv := httptest.NewServer(s.Handler())
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/contracts/orders/v1")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var c Contract
	if err := json.NewDecoder(resp.Body).Decode(&c); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if c.Service != "orders" || c.Version != "v1" {
		t.Fatalf("contract = %+v", c)
	}

	resp2, err := http.Get(srv.URL + "/contracts/orders/v9")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", resp2.StatusCode)
	}
}

func TestPutReplacesExistingVersion(t *testing.T) {
	s := NewStore()
	s.Put(Contract{Service: "a", Version: "v1", Request: json.RawMessage(`{}`)})
	s.Put(Contract{Service: "a", Version: "v1", Request: json.RawMessage(`{"type":"string"}`)})
	c, ok := s.Get("a", "v1")
	if !ok {
		t.Fatal("contract missing")
	}
	if string(c.Request) != `{"type":"string"}` {
		t.Fatalf("request = %s", c.Request)
	}
}

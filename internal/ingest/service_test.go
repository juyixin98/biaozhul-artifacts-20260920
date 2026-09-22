package ingest

import (
	"testing"
	"time"

	"anomalywatch/internal/models"
)

func TestContentHashStable(t *testing.T) {
	when := time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC)
	a := EventIn{
		EventID:    "e1",
		EmployeeID: 7,
		EventType:  models.EventTypeFileDownload,
		OccurredAt: when,
		Metadata:   models.JSONMap{"b": float64(2), "a": "x"},
	}
	b := EventIn{
		EventID:    "e1",
		EmployeeID: 7,
		EventType:  models.EventTypeFileDownload,
		OccurredAt: when,
		Metadata:   models.JSONMap{"a": "x", "b": float64(2)},
	}
	if ContentHash(a) != ContentHash(b) {
		t.Fatal("metadata key ordering changed the content hash")
	}
}

func TestContentHashIgnoresIdentity(t *testing.T) {
	when := time.Now().UTC()
	a := EventIn{EventID: "x", EmployeeID: 1, EventType: "login", OccurredAt: when}
	b := EventIn{EventID: "y", EmployeeID: 1, EventType: "login", OccurredAt: when}
	if ContentHash(a) != ContentHash(b) {
		t.Fatal("content hash must not depend on event_id")
	}
}

func TestContentHashDetectsDiffs(t *testing.T) {
	when := time.Now().UTC()
	base := EventIn{EventID: "x", EmployeeID: 1, EventType: "login", OccurredAt: when}
	cases := []EventIn{
		{EventID: "x", EmployeeID: 2, EventType: "login", OccurredAt: when},
		{EventID: "x", EmployeeID: 1, EventType: "usb", OccurredAt: when},
		{EventID: "x", EmployeeID: 1, EventType: "login", OccurredAt: when.Add(time.Second)},
		{EventID: "x", EmployeeID: 1, EventType: "login", OccurredAt: when, Metadata: models.JSONMap{"ip": "1.1.1.1"}},
	}
	h0 := ContentHash(base)
	for i, c := range cases {
		if ContentHash(c) == h0 {
			t.Fatalf("case %d unexpectedly hashed equal", i)
		}
	}
}

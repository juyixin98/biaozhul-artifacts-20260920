package client

import (
	"strings"
	"testing"
)

func TestReadSSEParsesEvents(t *testing.T) {
	t.Parallel()
	stream := "event: open\ndata: {\"id\":\"s-0001\"}\n\n" +
		"event: tick\ndata: {\"n\":1}\n\n" +
		"event: cancelled\ndata: {\"ticks\":4}\n\n"

	events := readSSE(strings.NewReader(stream))
	if len(events) != 3 {
		t.Fatalf("events=%d, want 3: %+v", len(events), events)
	}
	want := []string{"open", "tick", "cancelled"}
	for i, w := range want {
		if events[i].Event != w {
			t.Fatalf("event %d name=%q, want %q", i, events[i].Event, w)
		}
		if len(events[i].Data) == 0 {
			t.Fatalf("event %d has empty data", i)
		}
	}
	if string(events[2].Data) != `{"ticks":4}` {
		t.Fatalf("last data=%s", events[2].Data)
	}
}

func TestReadSSEHandlesEmptyStream(t *testing.T) {
	t.Parallel()
	if events := readSSE(strings.NewReader("")); len(events) != 0 {
		t.Fatalf("events=%d, want 0", len(events))
	}
}

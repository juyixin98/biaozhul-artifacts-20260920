package client

import (
	"bufio"
	"encoding/json"
	"io"
	"strings"
	"time"
)

// readSSE parses a server-sent-events stream into discrete events. It stops at
// EOF (the server closes the stream with a terminal event) or read error.
func readSSE(r io.Reader) []StreamEvent {
	var events []StreamEvent
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)

	var event string
	var dataParts []string
	flush := func() {
		if event == "" && len(dataParts) == 0 {
			return
		}
		ev := StreamEvent{Event: event, At: time.Now()}
		data := strings.Join(dataParts, "\n")
		if json.Valid([]byte(data)) {
			ev.Data = json.RawMessage(data)
		} else if data != "" {
			ev.Data = json.RawMessage(`"` + strings.ReplaceAll(data, `"`, `\"`) + `"`)
		}
		events = append(events, ev)
		event, dataParts = "", nil
	}

	for sc.Scan() {
		line := sc.Text()
		switch {
		case line == "":
			flush()
		case strings.HasPrefix(line, "event:"):
			event = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
		case strings.HasPrefix(line, "data:"):
			dataParts = append(dataParts, strings.TrimSpace(strings.TrimPrefix(line, "data:")))
		}
	}
	flush()
	return events
}

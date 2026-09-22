// Command importer loads sample events into a running system. It demonstrates
// every detection rule:
//
//	anomalywatch-importer --file samples/events.json
//	anomalywatch-importer --scenario stat-spike --employee-id 3
//
// Times in the JSON file may be RFC3339 ("2026-09-19T13:00:00Z") or relative
// ("-2h", "-30m"); relative times are resolved against submission time, which
// keeps samples inside the allowed backfill window.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

type eventIn struct {
	EventID    string         `json:"event_id"`
	EmployeeID uint64         `json:"employee_id"`
	EventType  string         `json:"event_type"`
	OccurredAt string         `json:"occurred_at"`
	Metadata   map[string]any `json:"metadata,omitempty"`
}

type fileFormat struct {
	Source string    `json:"source"`
	Events []eventIn `json:"events"`
}

func main() {
	base := flag.String("url", envOr("BASE_URL", "http://127.0.0.1:8080"), "server base URL")
	apiKey := flag.String("key", envOr("API_KEY", "admin-local-key"), "API key")
	file := flag.String("file", "samples/events.json", "JSON file to import")
	scenario := flag.String("scenario", "", "generate a built-in scenario: stat-spike")
	empID := flag.Uint("employee-id", 3, "employee id for generated scenarios")
	empTZ := flag.String("emp-tz", "Europe/London", "IANA time zone of the scenario employee")
	batch := flag.Int("batch", 500, "batch size when uploading")
	flag.Parse()

	var doc fileFormat
	switch {
	case *scenario != "":
		doc = buildScenario(*scenario, uint64(*empID), *empTZ)
	default:
		raw, err := os.ReadFile(*file)
		if err != nil {
			fatal("read file: %v", err)
		}
		if err := json.Unmarshal(raw, &doc); err != nil {
			fatal("parse file: %v", err)
		}
	}

	resolveTimes(doc.Events, time.Now().UTC())

	if err := upload(*base, *apiKey, doc, *batch); err != nil {
		fatal("%v", err)
	}
	fmt.Printf("uploaded %d events from source %q\n", len(doc.Events), doc.Source)
}

// buildScenario generates built-in data. times are built in the seeded
// employee's time zone (looked up via --emp-tz) so "10:00 local" stays daytime.
func buildScenario(name string, empID uint64, empTZ string) fileFormat {
	doc := fileFormat{Source: "scenario-" + name}
	loc, err := time.LoadLocation(empTZ)
	if err != nil {
		fatal("unknown --emp-tz %q: %v", empTZ, err)
	}
	localMidnight := func(daysAgo int) time.Time {
		y, m, d := time.Now().In(loc).AddDate(0, 0, -daysAgo).Date()
		return time.Date(y, m, d, 0, 0, 0, 0, loc)
	}
	switch name {
	case "stat-spike":
		// 29 quiet baseline days (5 logins each, spread across the working day),
		// then a 40-event spike on the most recent completed day. With a
		// constant baseline and min_samples=10, the spike is a clear >2.5
		// standard-deviation upward anomaly (zero baseline variance).
		day := 0
		for i := 29; i >= 1; i-- {
			when := localMidnight(i)
			for j := 0; j < 5; j++ {
				day++
				doc.Events = append(doc.Events, eventIn{
					EventID:    fmt.Sprintf("stat-%03d", day),
					EmployeeID: empID,
					EventType:  "login",
					// 09:00..13:00 local, well inside the working day.
					OccurredAt: when.Add(9*time.Hour + time.Duration(j)*time.Hour).Format(time.RFC3339),
				})
			}
		}
		when := localMidnight(1) // most recent completed local day
		for j := 0; j < 40; j++ {
			day++
			doc.Events = append(doc.Events, eventIn{
				EventID:    fmt.Sprintf("stat-%03d", day),
				EmployeeID: empID,
				EventType:  "login",
				OccurredAt: when.Add(9*time.Hour + time.Duration(j%8)*30*time.Minute).Format(time.RFC3339),
			})
		}
	default:
		fatal("unknown scenario %q", name)
	}
	return doc
}

func resolveTimes(events []eventIn, now time.Time) {
	for i := range events {
		s := strings.TrimSpace(events[i].OccurredAt)
		if strings.HasPrefix(s, "-") {
			d, err := time.ParseDuration(s)
			if err != nil {
				fatal("bad relative time %q: %v", s, err)
			}
			events[i].OccurredAt = now.Add(d).Format(time.RFC3339)
			continue
		}
		if _, err := time.Parse(time.RFC3339, s); err != nil {
			fatal("bad occurred_at %q: %v", s, err)
		}
	}
}

func upload(base, key string, doc fileFormat, batchSize int) error {
	for start := 0; start < len(doc.Events); start += batchSize {
		end := start + batchSize
		if end > len(doc.Events) {
			end = len(doc.Events)
		}
		body, err := json.Marshal(map[string]any{
			"source": doc.Source,
			"events": doc.Events[start:end],
		})
		if err != nil {
			return err
		}
		req, err := http.NewRequest(http.MethodPost, strings.TrimRight(base, "/")+"/api/v1/events/batch", bytes.NewReader(body))
		if err != nil {
			return err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-API-Key", key)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			return err
		}
		respBody, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusAccepted && resp.StatusCode != http.StatusConflict {
			return fmt.Errorf("upload %d-%d: HTTP %d: %s", start, end, resp.StatusCode, string(respBody))
		}
		fmt.Printf("batch %d-%d: %s\n", start, end, resp.Status)
	}
	return nil
}

func envOr(k, def string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return def
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "importer: "+format+"\n", args...)
	os.Exit(1)
}

var _ = strconv.Atoi

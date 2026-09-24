// Package metrics is the real HTTP client used by the decision engine to pull
// error/latency samples from the (stub) metrics service.
package metrics

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// Bucket is one time-slice of raw observations returned by the metrics service.
type Bucket struct {
	T         time.Time `json:"t"`
	Samples   int64     `json:"samples"`
	Errors    int64     `json:"errors"`
	LatencyMS []float64 `json:"latency_ms"`
}

// TickMS is the stub bucket width the decision engine assumes before a window
// is fetched (the actual width is always re-read from each response).
const TickMS int64 = 200

// Window is one /metrics response.
type Window struct {
	ReleaseID   string    `json:"release_id"`
	Scenario    string    `json:"scenario"`
	TickMS      int64     `json:"tick_ms"`
	WindowStart time.Time `json:"window_start"`
	WindowEnd   time.Time `json:"window_end"`
	// Available is false when the metrics service explicitly reports it has no
	// data for the requested window (blackout). The engine treats this as
	// UNKNOWN, never as healthy.
	Available bool     `json:"available"`
	Buckets   []Bucket `json:"buckets"`
}

// Client fetches metric windows over real HTTP.
type Client struct {
	BaseURL string
	HTTP    *http.Client
}

func NewClient(baseURL string) *Client {
	return &Client{
		BaseURL: baseURL,
		HTTP:    &http.Client{Timeout: 5 * time.Second},
	}
}

// ErrUnavailable is returned when the stub reports a blackout for the whole
// window (HTTP 503). Callers map it to the metrics_missing reason.
type ErrUnavailable struct{ Msg string }

func (e *ErrUnavailable) Error() string { return "metrics unavailable: " + e.Msg }

// Fetch pulls the buckets whose timestamp lies in [from, to). createdAt anchors
// the stub's scenario timeline (elapsed time since release start).
func (c *Client) Fetch(ctx context.Context, releaseID, scenario string, from, to, createdAt time.Time) (*Window, error) {
	u, err := url.Parse(c.BaseURL + "/metrics")
	if err != nil {
		return nil, err
	}
	q := u.Query()
	q.Set("release_id", releaseID)
	q.Set("scenario", scenario)
	q.Set("from", from.UTC().Format(time.RFC3339Nano))
	q.Set("to", to.UTC().Format(time.RFC3339Nano))
	q.Set("created_at", createdAt.UTC().Format(time.RFC3339Nano))
	u.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, fmt.Errorf("metrics call failed: %w", err)
	}
	defer resp.Body.Close()

	var w Window
	if resp.StatusCode == http.StatusServiceUnavailable {
		_ = json.NewDecoder(resp.Body).Decode(&w)
		if w.TickMS == 0 {
			w.TickMS = 200
		}
		return &w, &ErrUnavailable{Msg: "service returned 503 for window " + from.Format(time.RFC3339) + ".." + to.Format(time.RFC3339)}
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("metrics service returned HTTP %d", resp.StatusCode)
	}
	if err := json.NewDecoder(resp.Body).Decode(&w); err != nil {
		return nil, fmt.Errorf("decoding metrics response: %w", err)
	}
	if w.TickMS <= 0 {
		return nil, fmt.Errorf("metrics response has non-positive tick_ms=%s", strconv.FormatInt(w.TickMS, 10))
	}
	return &w, nil
}

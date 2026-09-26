// Package client is the fault-injection load client for the shutdown demo
// service. It sends long/stream/background requests, sets dependency faults,
// polls the split readiness/liveness probes, triggers (possibly repeated)
// shutdown signals and records structured per-action results.
package client

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Observation is one structured client-side result.
type Observation struct {
	Target     string            `json:"target"`
	Action     string            `json:"action"`
	StatusCode int               `json:"statusCode"`
	Outcome    string            `json:"outcome,omitempty"`
	Detail     string            `json:"detail,omitempty"`
	Body       json.RawMessage   `json:"body,omitempty"`
	Events     []StreamEvent     `json:"events,omitempty"`
	At         time.Time         `json:"at"`
	Headers    map[string]string `json:"headers,omitempty"`
}

// jsonBody keeps Body always-valid JSON, quoting plain-text responses.
func jsonBody(raw []byte) json.RawMessage {
	if json.Valid(raw) {
		return raw
	}
	q, _ := json.Marshal(string(raw))
	return q
}

// StreamEvent is one parsed SSE event from /stream.
type StreamEvent struct {
	Event string          `json:"event"`
	Data  json.RawMessage `json:"data"`
	At    time.Time       `json:"at"`
}

// Client talks to one server instance.
type Client struct {
	BaseURL  string // traffic listener
	AdminURL string // probe/admin listener
	HTTP     *http.Client
}

func New(baseURL, adminURL string) *Client {
	return &Client{
		BaseURL:  baseURL,
		AdminURL: adminURL,
		HTTP:     &http.Client{Timeout: 30 * time.Second},
	}
}

// SetFault configures the in-process fake dependency.
func (c *Client) SetFault(ctx context.Context, latency time.Duration, fail, hang bool) (Observation, error) {
	payload, _ := json.Marshal(map[string]any{
		"latencyMs": latency.Milliseconds(),
		"fail":      fail,
		"hang":      hang,
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.AdminURL+"/fault", bytes.NewReader(payload))
	if err != nil {
		return Observation{}, err
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return Observation{Target: "admin", Action: "set-fault", At: time.Now()}, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	return Observation{
		Target: "admin", Action: "set-fault", StatusCode: resp.StatusCode,
		Body: jsonBody(raw), At: time.Now(),
	}, nil
}

// ReleaseHangs unblocks hung dependency calls.
func (c *Client) ReleaseHangs(ctx context.Context) (Observation, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, c.AdminURL+"/fault/release", nil)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return Observation{Target: "admin", Action: "release-hangs", At: time.Now()}, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	return Observation{Target: "admin", Action: "release-hangs",
		StatusCode: resp.StatusCode, Body: jsonBody(raw), At: time.Now()}, nil
}

// SendWork issues one long request and parses its outcome envelope.
func (c *Client) SendWork(ctx context.Context) (Observation, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/work", nil)
	if err != nil {
		return Observation{}, err
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return Observation{Target: "traffic", Action: "work", At: time.Now(),
			Outcome: "request-error", Detail: err.Error()}, nil
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	obs := Observation{
		Target: "traffic", Action: "work", StatusCode: resp.StatusCode,
		Body: jsonBody(raw), At: time.Now(),
	}
	var env struct {
		Outcome string `json:"outcome"`
		Error   string `json:"error"`
	}
	if json.Unmarshal(raw, &env) == nil {
		obs.Outcome = env.Outcome
		obs.Detail = env.Error
	}
	return obs, nil
}

// OpenStream consumes /stream until the server closes the stream.
func (c *Client) OpenStream(ctx context.Context, duration time.Duration) (Observation, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		c.BaseURL+"/stream?duration="+duration.String(), nil)
	if err != nil {
		return Observation{}, err
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return Observation{Target: "traffic", Action: "stream", At: time.Now(),
			Outcome: "request-error", Detail: err.Error()}, nil
	}
	defer resp.Body.Close()

	obs := Observation{
		Target: "traffic", Action: "stream", StatusCode: resp.StatusCode, At: time.Now(),
		Headers: map[string]string{"Content-Type": resp.Header.Get("Content-Type")},
	}
	obs.Events = readSSE(resp.Body)
	for _, ev := range obs.Events {
		if ev.Event == "done" {
			obs.Outcome = "completed"
		}
		if ev.Event == "cancelled" {
			obs.Outcome = "cancelled"
		}
	}
	if obs.Outcome == "" {
		obs.Outcome = "ended-without-terminal-event"
	}
	return obs, nil
}

// SpawnBackground requests one detached background job.
func (c *Client) SpawnBackground(ctx context.Context, duration time.Duration) (Observation, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		c.BaseURL+"/bg?duration="+duration.String(), nil)
	if err != nil {
		return Observation{}, err
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return Observation{Target: "traffic", Action: "bg", At: time.Now(),
			Outcome: "request-error", Detail: err.Error()}, nil
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	obs := Observation{Target: "traffic", Action: "bg", StatusCode: resp.StatusCode, Body: jsonBody(raw), At: time.Now()}
	var env struct {
		Outcome string `json:"outcome"`
		Error   string `json:"error"`
	}
	if json.Unmarshal(raw, &env) == nil {
		if env.Outcome != "" {
			obs.Outcome = env.Outcome
		} else if resp.StatusCode == http.StatusAccepted {
			obs.Outcome = "accepted"
		}
		obs.Detail = env.Error
	}
	return obs, nil
}

// ProbeResult is one readiness/liveness sample.
type ProbeResult struct {
	Kind       string    `json:"kind"`
	StatusCode int       `json:"statusCode"`
	Ready      *bool     `json:"ready,omitempty"`
	Alive      *bool     `json:"alive,omitempty"`
	Phase      string    `json:"phase,omitempty"`
	At         time.Time `json:"at"`
}

// Probe samples one of /readyz or /livez.
func (c *Client) Probe(ctx context.Context, kind string) (ProbeResult, error) {
	u := c.AdminURL + "/" + kind
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return ProbeResult{Kind: kind, At: time.Now()}, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	p := ProbeResult{Kind: kind, StatusCode: resp.StatusCode, At: time.Now()}
	var env struct {
		Ready *bool  `json:"ready"`
		Alive *bool  `json:"alive"`
		Phase string `json:"phase"`
	}
	if json.Unmarshal(raw, &env) == nil {
		p.Ready = env.Ready
		p.Alive = env.Alive
		p.Phase = env.Phase
	}
	return p, nil
}

// TriggerShutdown fires signals (repeated if signals > 1) and returns the
// finalized server-side report embedded in the HTTP response.
func (c *Client) TriggerShutdown(ctx context.Context, signals int) (json.RawMessage, Observation, error) {
	u := fmt.Sprintf("%s/trigger-shutdown?signals=%d", c.AdminURL, signals)
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, u, nil)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, Observation{Target: "admin", Action: "trigger-shutdown", At: time.Now(),
			Outcome: "request-error", Detail: err.Error()}, nil
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	return raw, Observation{Target: "admin", Action: "trigger-shutdown",
		StatusCode: resp.StatusCode, Body: jsonBody(raw), At: time.Now()}, nil
}

// GetReport fetches the live structured report while shutdown is in progress.
func (c *Client) GetReport(ctx context.Context) (json.RawMessage, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, c.AdminURL+"/report", nil)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return io.ReadAll(resp.Body)
}

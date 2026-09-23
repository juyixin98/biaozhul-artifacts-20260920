// Package webhook delivers alert ENTER/RECOVER notifications to a
// subscriber URL. Each request body is genuinely signed with HMAC-SHA256
// (timestamp bound to the payload); the companion CLI command
// `sensorctl recv` verifies those signatures with the same code path.
package webhook

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"time"

	"sensorhealth/internal/clock"
	"sensorhealth/internal/cryptox"
	"sensorhealth/internal/engine"
	"sensorhealth/internal/store"
)

// Sink implements engine.Sink with a signed HTTP POST plus bounded retries.
type Sink struct {
	url      string
	secret   string
	client   *http.Client
	st       *store.Store
	clk      clock.Clock
	attempts int
	backoff  time.Duration
}

// NewSink builds a sink. When url is empty, Notify is a no-op (deliveries
// disabled) but alerts still persist locally.
func NewSink(url, secret string, st *store.Store, clk clock.Clock) *Sink {
	return &Sink{
		url:      url,
		secret:   secret,
		client:   &http.Client{Timeout: 5 * time.Second},
		st:       st,
		clk:      clk,
		attempts: 3,
		backoff:  200 * time.Millisecond,
	}
}

// Notify signs and POSTs the notification, recording every attempt.
func (s *Sink) Notify(ctx context.Context, n engine.Notification) {
	if s.url == "" {
		return
	}
	payload, err := json.Marshal(n)
	if err != nil {
		s.record(ctx, n, 0, 0, err)
		return
	}
	var lastStatus int
	var lastErr error
	for attempt := 1; attempt <= s.attempts; attempt++ {
		ts := s.clk.Now().UTC()
		sig := cryptox.SignPayload(s.secret, ts, payload)
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.url, bytes.NewReader(payload))
		if err != nil {
			lastErr = err
			s.record(ctx, n, attempt, 0, err)
			break
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Sensorhealth-Timestamp", ts.Format(time.RFC3339Nano))
		req.Header.Set("X-Sensorhealth-Signature", "sha256="+sig)

		resp, err := s.client.Do(req)
		if err != nil {
			lastErr, lastStatus = err, 0
			s.record(ctx, n, attempt, 0, err)
		} else {
			lastStatus = resp.StatusCode
			resp.Body.Close()
			if 200 <= resp.StatusCode && resp.StatusCode < 300 {
				s.record(ctx, n, attempt, resp.StatusCode, nil)
				return
			}
			lastErr = errUnexpectedStatus(resp.StatusCode)
			s.record(ctx, n, attempt, resp.StatusCode, lastErr)
		}
		if attempt < s.attempts {
			select {
			case <-ctx.Done():
				return
			case <-time.After(s.backoff * time.Duration(attempt)):
			}
		}
	}
	_ = lastStatus
	_ = lastErr
}

func (s *Sink) record(ctx context.Context, n engine.Notification, attempt, status int, err error) {
	d := store.Delivery{
		EventID:    n.Event.ID,
		Phase:      n.Phase,
		URL:        s.url,
		OK:         err == nil && status >= 200 && status < 300,
		StatusCode: status,
		Attempt:    attempt,
		CreatedAt:  s.clk.Now().UTC(),
	}
	if err != nil {
		d.Err = err.Error()
	}
	_ = s.st.RecordDelivery(ctx, d)
}

type httpStatusError int

func (e httpStatusError) Error() string { return "unexpected HTTP status: " + http.StatusText(int(e)) }

func errUnexpectedStatus(code int) error { return httpStatusError(code) }

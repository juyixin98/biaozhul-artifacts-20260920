package service

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"sensorhealth/internal/config"
	"sensorhealth/internal/domain"
	"sensorhealth/internal/store"
)

// testHarness bundles a store, a controllable clock and the service.
type testHarness struct {
	t   *testing.T
	st  *store.Store
	svc *Service
	now time.Time
	dir string
}

func newHarness(t *testing.T) *testHarness {
	t.Helper()
	dir := t.TempDir()
	dsn := filepath.Join(dir, "test.db")
	st, err := store.Open(context.Background(), dsn)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	h := &testHarness{t: t, st: st, dir: dir, now: time.Date(2026, 9, 23, 10, 0, 0, 0, time.UTC)}
	h.svc = New(st, func() time.Time { return h.now })
	t.Cleanup(func() { st.Close() })
	return h
}

// reopen closes and reopens the SAME database file, simulating a process
// restart; state must survive.
func (h *testHarness) reopen() {
	if err := h.st.Close(); err != nil {
		h.t.Fatalf("close: %v", err)
	}
	st, err := store.Open(context.Background(), filepath.Join(h.dir, "test.db"))
	if err != nil {
		h.t.Fatalf("reopen: %v", err)
	}
	h.st = st
	h.svc = New(st, func() time.Time { return h.now })
}

func (h *testHarness) advance(d time.Duration) { h.now = h.now.Add(d) }

// registerType registers a device with an explicit config.
func (h *testHarness) registerType(id, devType string, c domain.RuleConfig) {
	h.t.Helper()
	c.DeviceType = devType
	if _, err := h.svc.UpdateConfig(context.Background(), c); err != nil {
		h.t.Fatalf("update config: %v", err)
	}
	if _, _, err := h.svc.RegisterDevice(context.Background(), id, devType); err != nil {
		h.t.Fatalf("register: %v", err)
	}
}

// sample sends one sample; sampledAt defaults to "now" unless provided.
func (h *testHarness) sample(id string, seq int64, value string, sampledAt ...time.Time) {
	h.t.Helper()
	st := h.now
	if len(sampledAt) > 0 {
		st = sampledAt[0]
	}
	res, err := h.svc.IngestBatch(context.Background(), id, []IncomingMessage{
		{Seq: seq, Value: value, SampledAt: st, ReceivedAt: h.now},
	})
	if err != nil {
		h.t.Fatalf("ingest sample seq=%d: %v", seq, err)
	}
	if res.Accepted != 1 {
		h.t.Fatalf("expected 1 accepted, got %+v", res)
	}
}

func (h *testHarness) heartbeat(id string) {
	h.t.Helper()
	res, err := h.svc.IngestBatch(context.Background(), id, []IncomingMessage{
		{IsHeartbeat: true, SampledAt: h.now, ReceivedAt: h.now},
	})
	if err != nil {
		h.t.Fatalf("heartbeat: %v", err)
	}
	if res.Accepted != 1 {
		h.t.Fatalf("heartbeat not accepted: %+v", res)
	}
}

func (h *testHarness) openAlerts(id string, kind domain.Kind) []domain.Alert {
	h.t.Helper()
	alerts, err := h.st.ListAlerts(context.Background(), id, domain.StatusOpen, kind, 10)
	if err != nil {
		h.t.Fatalf("list alerts: %v", err)
	}
	return alerts
}

func (h *testHarness) recoveredAlerts(id string, kind domain.Kind) []domain.Alert {
	h.t.Helper()
	alerts, err := h.st.ListAlerts(context.Background(), id, domain.StatusRecovered, kind, 10)
	if err != nil {
		h.t.Fatalf("list alerts: %v", err)
	}
	return alerts
}

func fastConfig(devType string) domain.RuleConfig {
	c := config.DefaultRuleConfig(devType, 0)
	c.StaleEnterTimeout = domain.Duration(10 * time.Second)
	c.StaleRecoverTimeout = domain.Duration(3 * time.Second)
	c.FrozenEnterCount = 4
	c.FrozenEnterMinDuration = domain.Duration(20 * time.Millisecond)
	c.FrozenRecoverCount = 2
	c.MissingEnterCount = 3
	c.MissingRecoverCount = 1
	c.BackfillLookback = 50
	return c
}

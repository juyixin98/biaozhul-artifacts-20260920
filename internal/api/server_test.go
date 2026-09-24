package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"sensorhealth/internal/crypto"
	"sensorhealth/internal/domain"
	"sensorhealth/internal/service"
	"sensorhealth/internal/store"
)

type e2e struct {
	t      *testing.T
	ts     *httptest.Server
	signer *crypto.Signer
	st     *store.Store
	svc    *service.Service
	secret []byte
	admin  string
}

func newE2E(t *testing.T) *e2e {
	t.Helper()
	dir := t.TempDir()
	st, err := store.Open(context.Background(), filepath.Join(dir, "e2e.db"))
	if err != nil {
		t.Fatal(err)
	}
	svc := service.New(st, time.Now)
	secret := []byte("test-secret")
	signer := crypto.NewSigner(secret)
	srv := NewServer(svc, signer, "admin-token")
	ts := httptest.NewServer(srv.Routes())
	t.Cleanup(func() { ts.Close(); st.Close() })
	return &e2e{t: t, ts: ts, signer: signer, st: st, svc: svc, secret: secret, admin: "admin-token"}
}

func (e *e2e) adminDo(method, path string, body any, out any) int {
	e.t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, _ := http.NewRequest(method, e.ts.URL+path, rdr)
	req.Header.Set("Authorization", "Bearer "+e.admin)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	if out != nil {
		json.NewDecoder(resp.Body).Decode(out)
	}
	return resp.StatusCode
}

// signedIngest posts a signed envelope with fresh headers.
func (e *e2e) signedIngest(env Envelope) (int, map[string]any) {
	e.t.Helper()
	raw, _ := json.Marshal(env)
	now := time.Now()
	sig := crypto.Sign(e.secret, http.MethodPost, "/api/v1/ingest", now, raw)
	req, _ := http.NewRequest(http.MethodPost, e.ts.URL+"/api/v1/ingest", bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Timestamp", now.UTC().Format(time.RFC3339Nano))
	req.Header.Set("X-Nonce", fmt.Sprintf("nonce-%d", time.Now().UnixNano()))
	req.Header.Set("X-Signature", sig)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	var m map[string]any
	json.NewDecoder(resp.Body).Decode(&m)
	return resp.StatusCode, m
}

func (e *e2e) register(id, typ string) {
	e.t.Helper()
	code := e.adminDo(http.MethodPost, "/admin/devices", map[string]string{"id": id, "type": typ}, nil)
	if code != http.StatusCreated {
		e.t.Fatalf("register status=%d", code)
	}
}

func (e *e2e) get(path string, out any) int {
	resp, err := http.Get(e.ts.URL + path)
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	if out != nil {
		json.NewDecoder(resp.Body).Decode(out)
	}
	return resp.StatusCode
}

func TestE2E_IngestAuthAndHealth(t *testing.T) {
	e := newE2E(t)
	e.register("dev1", "default")

	base := time.Now().UTC()
	env := Envelope{DeviceID: "dev1", Messages: []MsgDTO{
		{Seq: 1, Value: "21.5", SampledAt: base.Add(-2 * time.Second)},
		{Seq: 2, Value: "21.6", SampledAt: base.Add(-time.Second)},
		{Kind: "heartbeat", SampledAt: base},
	}}
	code, body := e.signedIngest(env)
	if code != http.StatusAccepted {
		t.Fatalf("ingest status=%d body=%v", code, body)
	}
	if body["accepted"].(float64) != 3 {
		t.Fatalf("expected 3 accepted, got %v", body["accepted"])
	}

	// Unsigned request must be rejected.
	raw, _ := json.Marshal(env)
	resp, err := http.Post(e.ts.URL+"/api/v1/ingest", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("unsigned ingest status=%d want 401", resp.StatusCode)
	}
	resp.Body.Close()

	// Health endpoint.
	var h domain.Health
	if code := e.get("/api/v1/devices/dev1/health", &h); code != http.StatusOK {
		t.Fatalf("health status=%d", code)
	}
	if !h.Healthy || h.ConfigVersion == 0 || h.LastSeq != 2 {
		t.Fatalf("unexpected health: %+v", h)
	}
}

func TestE2E_UnknownDeviceRejected(t *testing.T) {
	e := newE2E(t)
	env := Envelope{DeviceID: "ghost", Messages: []MsgDTO{
		{Seq: 1, Value: "1", SampledAt: time.Now()},
	}}
	code, body := e.signedIngest(env)
	if code != http.StatusNotFound {
		t.Fatalf("want 404, got %d %v", code, body)
	}
}

func TestE2E_AdminAuth(t *testing.T) {
	e := newE2E(t)
	req, _ := http.NewRequest(http.MethodPost, e.ts.URL+"/admin/sweep", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("admin without token status=%d", resp.StatusCode)
	}
	resp.Body.Close()
	if code := e.adminDo(http.MethodPost, "/admin/sweep", nil, nil); code != http.StatusOK {
		t.Fatalf("admin sweep status=%d", code)
	}
}

func TestE2E_ConfigVersionBumpsAndAppearsOnAlert(t *testing.T) {
	e := newE2E(t)
	e.register("dev2", "temp")

	// Tighten configs through the admin API so a frozen alert fires quickly.
	cfg := map[string]any{
		"stale_enter_timeout":       "10s",
		"stale_recover_timeout":     "3s",
		"frozen_enter_count":        2,
		"frozen_enter_min_duration": "1ms",
		"frozen_recover_count":      1,
		"missing_enter_count":       5,
		"missing_recover_count":     2,
		"backfill_lookback":         100,
	}
	var got domain.RuleConfig
	code := e.adminDo(http.MethodPut, "/admin/device-types/temp/config", cfg, &got)
	if code != http.StatusOK {
		t.Fatalf("put config status=%d", code)
	}
	if got.Version < 2 {
		t.Fatalf("config version should bump beyond default, got %d", got.Version)
	}

	base := time.Now().UTC()
	env := Envelope{DeviceID: "dev2", Messages: []MsgDTO{
		{Seq: 1, Value: "5", SampledAt: base.Add(-3 * time.Millisecond)},
		{Seq: 2, Value: "5", SampledAt: base.Add(-2 * time.Millisecond)},
	}}
	if code, body := e.signedIngest(env); code != http.StatusAccepted {
		t.Fatalf("ingest: %d %v", code, body)
	}

	var list struct {
		Alerts []domain.Alert `json:"alerts"`
	}
	if code := e.get("/api/v1/alerts?device_id=dev2&status=open&kind=frozen", &list); code != http.StatusOK {
		t.Fatalf("list alerts %d", code)
	}
	if len(list.Alerts) != 1 {
		t.Fatalf("expected 1 frozen alert, got %d", len(list.Alerts))
	}
	if list.Alerts[0].ConfigVersion < 2 {
		t.Fatalf("alert should carry new config version, got %d", list.Alerts[0].ConfigVersion)
	}
	rng := list.Alerts[0].TriggerRange
	if rng.SeqStart != 1 || rng.SeqEnd != 2 {
		t.Fatalf("trigger range wrong: %+v", rng)
	}
}

func TestE2E_BatchBackfillRecoversGap(t *testing.T) {
	e := newE2E(t)
	e.register("dev3", "g")
	// Tight gap config.
	cfg := map[string]any{
		"stale_enter_timeout":       "60s",
		"stale_recover_timeout":     "10s",
		"frozen_enter_count":        100,
		"frozen_enter_min_duration": "1m",
		"frozen_recover_count":      2,
		"missing_enter_count":       3,
		"missing_recover_count":     1,
		"backfill_lookback":         100,
	}
	e.adminDo(http.MethodPut, "/admin/device-types/g/config", cfg, nil)

	base := time.Now().UTC()
	// 1 then jump to 5 -> 3 missing.
	e.signedIngest(Envelope{DeviceID: "dev3", Messages: []MsgDTO{
		{Seq: 1, Value: "a", SampledAt: base},
		{Seq: 5, Value: "e", SampledAt: base.Add(40 * time.Millisecond)},
	}})
	var list struct {
		Alerts []domain.Alert `json:"alerts"`
	}
	e.get("/api/v1/alerts?device_id=dev3&status=open&kind=gap", &list)
	if len(list.Alerts) != 1 {
		t.Fatalf("expected open gap, got %d", len(list.Alerts))
	}

	// Backfill in one batch.
	e.signedIngest(Envelope{DeviceID: "dev3", Messages: []MsgDTO{
		{Seq: 4, Value: "d", SampledAt: base.Add(30 * time.Millisecond)},
		{Seq: 2, Value: "b", SampledAt: base.Add(10 * time.Millisecond)},
		{Seq: 3, Value: "c", SampledAt: base.Add(20 * time.Millisecond)},
	}})
	list.Alerts = nil
	e.get("/api/v1/alerts?device_id=dev3&status=open&kind=gap", &list)
	if len(list.Alerts) != 0 {
		t.Fatalf("gap should be recovered by batch backfill, still %d open", len(list.Alerts))
	}
}

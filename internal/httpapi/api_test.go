package httpapi_test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"sensorhealth/internal/clock"
	"sensorhealth/internal/cryptox"
	"sensorhealth/internal/engine"
	"sensorhealth/internal/httpapi"
	"sensorhealth/internal/model"
	"sensorhealth/internal/store"
	"sensorhealth/internal/webhook"
)

const (
	ingestSecret = "test-ingest-secret"
	adminToken   = "test-admin-token"
)

type apiHarness struct {
	t    *testing.T
	dir  string
	st   *store.Store
	eng  *engine.Engine
	vclk *clock.Virtual
	srv  *httptest.Server
}

func newAPI(t *testing.T) *apiHarness {
	t.Helper()
	dir := t.TempDir()
	st, err := store.Open(context.Background(), filepath.Join(dir, "api.db"))
	if err != nil {
		t.Fatal(err)
	}
	vclk := clock.NewVirtual(time.Date(2026, 9, 23, 10, 0, 0, 0, time.UTC))
	eng, err := engine.New(context.Background(), st, vclk)
	if err != nil {
		t.Fatal(err)
	}
	h := httpapi.NewHandler(eng, st, vclk, vclk, httpapi.Config{
		IngestSecret: ingestSecret, AdminToken: adminToken,
		ReplayWindow: time.Hour,
	})
	srv := httptest.NewServer(h)
	t.Cleanup(func() { srv.Close(); st.Close() })
	ah := &apiHarness{t: t, dir: dir, st: st, eng: eng, vclk: vclk, srv: srv}
	ah.putType(model.DeviceType{
		Type: "temperature", StaleEnterSec: 30, StaleRecoverSec: 10,
		FixedWindowCount: 3, GapRecoverGapless: 2, ClockSkewTolMs: 500,
	})
	return ah
}

func (a *apiHarness) putType(d model.DeviceType) model.DeviceType {
	a.t.Helper()
	body, _ := json.Marshal(d)
	req, _ := http.NewRequest(http.MethodPut,
		a.srv.URL+"/admin/config/device-types/"+d.Type, bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+adminToken)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		a.t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		a.t.Fatalf("put type %s: %s", resp.Status, b)
	}
	var out model.DeviceType
	json.NewDecoder(resp.Body).Decode(&out)
	return out
}

// signedIngest posts a batch with a genuine HMAC-SHA256 signature.
func (a *apiHarness) signedIngest(body []byte, ts time.Time, secret string) (int, map[string]any) {
	a.t.Helper()
	req, _ := http.NewRequest(http.MethodPost, a.srv.URL+"/v1/ingest", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Timestamp", ts.Format(time.RFC3339))
	req.Header.Set("X-Signature", "sha256="+cryptox.Sign(secret, ts.Unix(), body))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		a.t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out map[string]any
	_ = json.Unmarshal(raw, &out)
	return resp.StatusCode, out
}

// rawSignedIngest posts an arbitrary body carrying a signature computed
// over a different payload — used to prove tampering is rejected.
func (a *apiHarness) rawSignedWithSigOf(body, signedOver []byte, ts time.Time, secret string) int {
	a.t.Helper()
	sig := cryptox.Sign(secret, ts.Unix(), signedOver)
	req, _ := http.NewRequest(http.MethodPost, a.srv.URL+"/v1/ingest", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Timestamp", ts.Format(time.RFC3339))
	req.Header.Set("X-Signature", "sha256="+sig)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		a.t.Fatal(err)
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	return resp.StatusCode
}

func batch(msgs ...model.Message) []byte {
	b, _ := json.Marshal(map[string]any{"messages": msgs})
	return b
}

func dmsg(dev string, seq int64, sample time.Time, v float64) model.Message {
	return model.Message{DeviceID: dev, Type: "temperature", Seq: &seq,
		SampleTime: sample.Format(time.RFC3339Nano), Value: &v}
}

// A correctly signed request is accepted; tampered body, wrong secret and a
// stale timestamp are all rejected with 401.
func TestIngestSignatureEnforcement(t *testing.T) {
	a := newAPI(t)
	t0 := a.vclk.Now()
	body := batch(dmsg("s1", 1, t0, 20))

	if code, _ := a.signedIngest(body, t0, ingestSecret); code != http.StatusOK {
		t.Fatalf("valid signature: status = %d, want 200", code)
	}
	if code, _ := a.signedIngest(body, t0, "wrong-secret"); code != http.StatusUnauthorized {
		t.Fatalf("wrong secret: status = %d, want 401", code)
	}
	// A body delivered with a signature computed over a *different* payload
	// must be rejected (the signature cannot be forged without the secret).
	tampered := batch(dmsg("s2", 1, t0, 20))
	if code := a.rawSignedWithSigOf(tampered, body, t0, ingestSecret); code != http.StatusUnauthorized {
		t.Fatalf("tampered body: status = %d, want 401", code)
	}
	if code, _ := a.signedIngest(body, t0.Add(-2*time.Hour), ingestSecret); code != http.StatusUnauthorized {
		t.Fatalf("stale timestamp: status = %d, want 401", code)
	}
}

// Admin endpoints require the bearer token.
func TestAdminAuth(t *testing.T) {
	a := newAPI(t)
	resp, err := http.Post(a.srv.URL+"/admin/clock/advance", "application/json",
		bytes.NewReader([]byte(`{"advance_ms":1000}`)))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("unauthenticated admin call: status = %d, want 401", resp.StatusCode)
	}
}

// End-to-end through HTTP: fixed-value alert is queryable with its trigger
// interval and config version.
func TestHTTPEndToEndFixedAlert(t *testing.T) {
	a := newAPI(t)
	cfg := a.putType(model.DeviceType{
		Type: "temperature", StaleEnterSec: 30, StaleRecoverSec: 10,
		FixedWindowCount: 3, GapRecoverGapless: 2, ClockSkewTolMs: 500,
	})
	t0 := a.vclk.Now()
	body := batch(
		dmsg("s1", 1, t0, 20), dmsg("s1", 2, t0.Add(time.Second), 20),
		dmsg("s1", 3, t0.Add(2*time.Second), 20),
	)
	if code, out := a.signedIngest(body, a.vclk.Now(), ingestSecret); code != 200 {
		t.Fatalf("ingest status = %d body=%v", code, out)
	}

	resp, err := http.Get(a.srv.URL + "/v1/devices/s1")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var hh model.Health
	json.NewDecoder(resp.Body).Decode(&hh)
	fr := hh.Rules[model.RuleFixed]
	if fr.State != model.StateAlert {
		t.Fatalf("fixed rule state = %s, want ALERT", fr.State)
	}
	if hh.ConfigVersion != cfg.Version {
		t.Fatalf("health config version = %d, want %d", hh.ConfigVersion, cfg.Version)
	}

	resp2, err := http.Get(a.srv.URL + "/v1/events?device_id=s1&rule=fixed_value&open=true")
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	var listed struct {
		Events []model.Event `json:"events"`
	}
	json.NewDecoder(resp2.Body).Decode(&listed)
	if len(listed.Events) != 1 || listed.Events[0].StartSeq == nil || *listed.Events[0].StartSeq != 1 {
		t.Fatalf("events = %+v, want one fixed event starting at seq 1", listed.Events)
	}
}

// Advancing the virtual clock through the admin endpoint fires stale alerts;
// a subsequent ingest recovers them (enter and recovery thresholds differ).
func TestHTTPStaleViaClockAdvance(t *testing.T) {
	a := newAPI(t)
	t0 := a.vclk.Now()
	a.signedIngest(batch(dmsg("s1", 1, t0, 20)), a.vclk.Now(), ingestSecret)

	adv := func(body string) map[string]any {
		req, _ := http.NewRequest(http.MethodPost, a.srv.URL+"/admin/clock/advance",
			bytes.NewReader([]byte(body)))
		req.Header.Set("Authorization", "Bearer "+adminToken)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		if resp.StatusCode != 200 {
			raw, _ := io.ReadAll(resp.Body)
			t.Fatalf("advance: %s %s", resp.Status, raw)
		}
		var out map[string]any
		json.NewDecoder(resp.Body).Decode(&out)
		return out
	}
	out := adv(`{"advance_ms":35000}`)
	if out["stale_alerts_fired"].(float64) != 1 {
		t.Fatalf("stale fired = %v, want 1", out["stale_alerts_fired"])
	}
	// A fresh data message recovers stale immediately.
	a.signedIngest(batch(dmsg("s1", 2, a.vclk.Now(), 21)), a.vclk.Now(), ingestSecret)
	resp, err := http.Get(a.srv.URL + "/v1/devices/s1")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var hh model.Health
	json.NewDecoder(resp.Body).Decode(&hh)
	if hh.Rules[model.RuleStale].State != model.StateOK {
		t.Fatalf("stale = %s, want OK after new message", hh.Rules[model.RuleStale].State)
	}
}

// Alert state survives a full process restart: reopen the same SQLite file
// with a brand-new engine and the ALERT must still be reported.
func TestStateSurvivesRestart(t *testing.T) {
	a := newAPI(t)
	t0 := a.vclk.Now()
	a.signedIngest(batch(
		dmsg("s1", 1, t0, 20), dmsg("s1", 2, t0.Add(time.Second), 20),
		dmsg("s1", 3, t0.Add(2*time.Second), 20),
	), a.vclk.Now(), ingestSecret)
	a.srv.Close()
	a.st.Close()

	// Simulated restart: new store handle, fresh engine on a virtual clock
	// positioned in the future, same database file.
	st2, err := store.Open(context.Background(), filepath.Join(a.dir, "api.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	vclk2 := clock.NewVirtual(a.vclk.Now().Add(time.Minute))
	eng2, err := engine.New(context.Background(), st2, vclk2)
	if err != nil {
		t.Fatal(err)
	}
	hh, err := eng2.Health(context.Background(), "s1")
	if err != nil || hh == nil {
		t.Fatalf("health after restart: %v", err)
	}
	if hh.Rules[model.RuleFixed].State != model.StateAlert {
		t.Fatalf("fixed state after restart = %s, want ALERT (state must persist)", hh.Rules[model.RuleFixed].State)
	}
	// A restarted server runs the same periodic stale sweep; running it once
	// here stands in for that goroutine tick.
	fired, err := eng2.Sweep(context.Background())
	if err != nil || fired != 1 {
		t.Fatalf("post-restart sweep fired=%d err=%v, want 1", fired, err)
	}
	hh, err = eng2.Health(context.Background(), "s1")
	if hh.Rules[model.RuleStale].State != model.StateAlert {
		t.Fatalf("stale state after restart sweep = %s, want ALERT", hh.Rules[model.RuleStale].State)
	}
}

// The webhook sink really signs with HMAC-SHA256; the receiver verifying
// with the same secret accepts it, and rejects a wrong secret.
func TestWebhookSignatureRoundTrip(t *testing.T) {
	dir := t.TempDir()
	st, err := store.Open(context.Background(), filepath.Join(dir, "wh.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	vclk := clock.NewVirtual(time.Date(2026, 9, 23, 10, 0, 0, 0, time.UTC))
	eng, err := engine.New(context.Background(), st, vclk)
	if err != nil {
		t.Fatal(err)
	}

	var gotSig, gotTS string
	var gotBody []byte
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotSig = r.Header.Get("X-Sensorhealth-Signature")
		gotTS = r.Header.Get("X-Sensorhealth-Timestamp")
		gotBody, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()

	secret := "webhook-secret"
	eng.SetSink(webhook.NewSink(upstream.URL, secret, st, vclk))

	// Configure a quick fixed rule and trigger an alert.
	_, err = st.UpsertDeviceType(context.Background(), model.DeviceType{
		Type: "temperature", StaleEnterSec: 30, StaleRecoverSec: 10,
		FixedWindowCount: 2, GapRecoverGapless: 2, ClockSkewTolMs: 500,
	})
	if err != nil {
		t.Fatal(err)
	}
	t0 := vclk.Now()
	if _, err := eng.Ingest(context.Background(), []model.Message{
		dmsg("s1", 1, t0, 9), dmsg("s1", 2, t0.Add(time.Second), 9),
	}, engine.IngestOptions{}); err != nil {
		t.Fatal(err)
	}
	if gotSig == "" {
		t.Fatal("webhook received no request")
	}
	ts, err := time.Parse(time.RFC3339, gotTS)
	if err != nil {
		t.Fatalf("webhook timestamp: %v", err)
	}
	hexSig := gotSig
	if len(hexSig) > 7 && hexSig[:7] == "sha256=" {
		hexSig = hexSig[7:]
	}
	if !cryptox.Verify(secret, hexSig, ts.Unix(), gotBody) {
		t.Fatal("webhook HMAC does not verify with the configured secret")
	}
	if cryptox.Verify("other-secret", hexSig, ts.Unix(), gotBody) {
		t.Fatal("webhook HMAC unexpectedly verified with a wrong secret")
	}
}

func TestHealthz(t *testing.T) {
	a := newAPI(t)
	resp, err := http.Get(a.srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("healthz = %d", resp.StatusCode)
	}
}

package httpapi_test

import (
	"bufio"
	"bytes"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"mqttsub/internal/broker"
	"mqttsub/internal/httpapi"
	"mqttsub/internal/mqtt"
)

func setup(t *testing.T) (*httptest.Server, *broker.Broker, string) {
	t.Helper()
	b, err := broker.New(broker.Config{StorePath: filepath.Join(t.TempDir(), "s.json")})
	if err != nil {
		t.Fatal(err)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ln.Close(); _ = b.Shutdown() })
	go func() {
		for {
			nc, err := ln.Accept()
			if err != nil {
				return
			}
			go b.Serve(nc)
		}
	}()
	srv := httptest.NewServer(httpapi.Handler(b))
	t.Cleanup(srv.Close)
	return srv, b, ln.Addr().String()
}

func TestHealthAndStats(t *testing.T) {
	srv, _, _ := setup(t)
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("healthz status %d", resp.StatusCode)
	}

	resp2, err := http.Get(srv.URL + "/stats")
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusOK {
		t.Fatalf("stats status %d", resp2.StatusCode)
	}
	var stats map[string]any
	if err := json.NewDecoder(resp2.Body).Decode(&stats); err != nil {
		t.Fatal(err)
	}
}

func TestPublishReachesDurableOfflineSession(t *testing.T) {
	srv, _, mqttAddr := setup(t)

	// Establish a durable session + subscription over real MQTT.
	nc, err := net.Dial("tcp", mqttAddr)
	if err != nil {
		t.Fatal(err)
	}
	nc.Write(mqtt.EncodeConnect(&mqtt.ConnectPacket{ClientID: "http-demo", CleanSession: false}))
	readOne(t, nc) // CONNACK
	nc.Write(mqtt.EncodeSubscribe(1, []mqtt.Subscription{{Filter: "http/#", MaxQoS: 1}}))
	readOne(t, nc) // SUBACK
	nc.Close()

	// Wait until the broker observes the session as offline.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		r, _ := http.Get(srv.URL + "/sessions/http-demo")
		var inf struct {
			Online bool `json:"online"`
		}
		json.NewDecoder(r.Body).Decode(&inf)
		r.Body.Close()
		if !inf.Online {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}

	// Inject via HTTP.
	body := `{"topic":"http/a","qos":1,"payload":"via-http"}`
	resp, err := http.Post(srv.URL+"/publish", "application/json", bytes.NewBufferString(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("publish status %d", resp.StatusCode)
	}
	var pr map[string]any
	json.NewDecoder(resp.Body).Decode(&pr)
	if m := pr["matched_sessions"].(float64); m != 1 {
		t.Fatalf("matched_sessions=%v want 1", pr["matched_sessions"])
	}

	// Session view shows the queued message.
	resp2, err := http.Get(srv.URL + "/sessions/http-demo")
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	var info map[string]any
	json.NewDecoder(resp2.Body).Decode(&info)
	if pending := info["pending"].(float64); pending != 1 {
		t.Fatalf("pending=%v want 1", info["pending"])
	}
}

func TestPublishValidation(t *testing.T) {
	srv, _, _ := setup(t)
	post := func(body string) int {
		resp, err := http.Post(srv.URL+"/publish", "application/json", bytes.NewBufferString(body))
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	if code := post(`{"topic":"a/+","qos":1}`); code != http.StatusBadRequest {
		t.Fatalf("wildcard topic should be 400, got %d", code)
	}
	if code := post(`{"topic":"a","qos":2}`); code != http.StatusBadRequest {
		t.Fatalf("qos2 should be 400, got %d", code)
	}
	if code := post(`{bad json`); code != http.StatusBadRequest {
		t.Fatalf("bad json should be 400, got %d", code)
	}
}

func TestSessionDelete(t *testing.T) {
	srv, _, mqttAddr := setup(t)
	nc, err := net.Dial("tcp", mqttAddr)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	nc.Write(mqtt.EncodeConnect(&mqtt.ConnectPacket{ClientID: "doomed", CleanSession: false}))
	readOne(t, nc)

	req, _ := http.NewRequest(http.MethodDelete, srv.URL+"/sessions/doomed", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("delete status %d", resp.StatusCode)
	}
	resp2, _ := http.Get(srv.URL + "/sessions/doomed")
	resp2.Body.Close()
	if resp2.StatusCode != http.StatusNotFound {
		t.Fatalf("expected 404 after delete, got %d", resp2.StatusCode)
	}
}

func readOne(t *testing.T, nc net.Conn) *mqtt.Frame {
	t.Helper()
	nc.SetReadDeadline(time.Now().Add(2 * time.Second))
	fr, err := mqtt.ReadFrame(bufio.NewReader(nc))
	if err != nil {
		t.Fatal(err)
	}
	return fr
}

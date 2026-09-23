package httpapi

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"mqttsubset/mqtt"
)

func setupServer(t *testing.T) (*httptest.Server, *mqtt.Broker, string) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	broker, err := mqtt.NewBroker(t.TempDir(), log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = broker.Serve(ln) }()
	t.Cleanup(func() { _ = broker.Close() })

	srv := httptest.NewServer(Handler(broker, log.New(io.Discard, "", 0)))
	t.Cleanup(srv.Close)
	return srv, broker, ln.Addr().String()
}

func doDelete(t *testing.T, url string) int {
	t.Helper()
	req, err := http.NewRequest(http.MethodDelete, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	return resp.StatusCode
}

func TestHealthz(t *testing.T) {
	srv, _, _ := setupServer(t)
	resp, err := srv.Client().Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("status=%d", resp.StatusCode)
	}
	var body map[string]string
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body["status"] != "ok" {
		t.Fatalf("body=%v", body)
	}
}

func TestHTTPPublishAndSessions(t *testing.T) {
	srv, _, mqttAddr := setupServer(t)

	// Persistent subscriber over the real MQTT port.
	sub, err := net.DialTimeout("tcp", mqttAddr, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer sub.Close()
	connect := buildConnect("http-sub", false, 0)
	if _, err := sub.Write(connect); err != nil {
		t.Fatal(err)
	}
	ack := make([]byte, 4)
	if _, err := readFull(sub, ack); err != nil {
		t.Fatal(err)
	}
	subWrite(sub, buildSubscribe(1, 1, "http/evt"))
	suback := make([]byte, 5)
	if _, err := readFull(sub, suback); err != nil {
		t.Fatal(err)
	}

	// Publish via HTTP.
	reqBody := `{"topic":"http/evt","qos":1,"payload_text":"over-http"}`
	resp, err := srv.Client().Post(srv.URL+"/v1/publish", "application/json", strings.NewReader(reqBody))
	if err != nil {
		t.Fatal(err)
	}
	var pubResp map[string]any
	json.NewDecoder(resp.Body).Decode(&pubResp)
	resp.Body.Close()
	if resp.StatusCode != 202 {
		t.Fatalf("publish status=%d", resp.StatusCode)
	}
	if pubResp["matched_subscribers"].(float64) != 1 {
		t.Fatalf("matched=%v", pubResp["matched_subscribers"])
	}

	// The MQTT subscriber should have received the message.
	pkt := make([]byte, 256)
	_ = sub.SetReadDeadline(time.Now().Add(2 * time.Second))
	n, err := sub.Read(pkt)
	if err != nil {
		t.Fatalf("subscriber read: %v", err)
	}
	if !bytes.Contains(pkt[:n], []byte("over-http")) {
		t.Fatalf("delivered packet missing payload: % x", pkt[:n])
	}

	// Session listing shows the persistent subscriber with one inflight.
	var info mqtt.SessionInfo
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		r2, err := srv.Client().Get(srv.URL + "/v1/sessions/http-sub")
		if err != nil {
			t.Fatal(err)
		}
		json.NewDecoder(r2.Body).Decode(&info)
		r2.Body.Close()
		if info.InflightCount == 1 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if info.InflightCount != 1 || info.Subscriptions["http/evt"] != 1 {
		t.Fatalf("session info wrong: %+v", info)
	}

	// Deleting the session returns 200; a second delete 404.
	if code := doDelete(t, srv.URL+"/v1/sessions/http-sub"); code != 200 {
		t.Fatalf("delete status=%d", code)
	}
	if code := doDelete(t, srv.URL+"/v1/sessions/http-sub"); code != 404 {
		t.Fatalf("second delete status=%d want 404", code)
	}
}

func TestHTTPPublishValidation(t *testing.T) {
	srv, _, _ := setupServer(t)

	bad := []string{
		`{"topic":"","qos":1}`,
		`{"topic":"a/+","qos":1}`, // wildcard is not a publish topic
		`{"topic":"a","qos":2}`,   // qos 2 unsupported
		`{not json`,
	}
	for _, body := range bad {
		resp, err := srv.Client().Post(srv.URL+"/v1/publish", "application/json", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != 400 {
			t.Fatalf("body=%s -> status=%d, want 400", body, resp.StatusCode)
		}
	}

	// QoS 0 publish with zero subscribers is still accepted.
	resp, err := srv.Client().Post(srv.URL+"/v1/publish", "application/json",
		strings.NewReader(`{"topic":"no/match","qos":0,"payload_text":"x"}`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 202 {
		t.Fatalf("status=%d", resp.StatusCode)
	}
}

// ---- minimal frame builders for the test's raw MQTT subscriber ----

func encLen(n int) []byte {
	var out []byte
	for {
		b := byte(n % 128)
		n /= 128
		if n > 0 {
			b |= 0x80
		}
		out = append(out, b)
		if n == 0 {
			return out
		}
	}
}

func putStr(dst []byte, s string) []byte {
	dst = append(dst, byte(len(s)>>8), byte(len(s)))
	return append(dst, s...)
}

func buildConnect(id string, clean bool, ka uint16) []byte {
	flags := byte(0)
	if clean {
		flags = 0x02
	}
	body := putStr(nil, "MQTT")
	body = append(body, 4, flags, byte(ka>>8), byte(ka))
	body = putStr(body, id)
	out := []byte{mqtt.TypeCONNECT << 4}
	out = append(out, encLen(len(body))...)
	return append(out, body...)
}

func buildSubscribe(pid uint16, qos byte, filter string) []byte {
	body := []byte{byte(pid >> 8), byte(pid)}
	body = putStr(body, filter)
	body = append(body, qos)
	out := []byte{0x82}
	out = append(out, encLen(len(body))...)
	return append(out, body...)
}

func subWrite(c net.Conn, b []byte) {
	if _, err := c.Write(b); err != nil {
		panic(err)
	}
}

func readFull(c net.Conn, buf []byte) (int, error) {
	total := 0
	for total < len(buf) {
		n, err := c.Read(buf[total:])
		total += n
		if err != nil {
			return total, err
		}
	}
	return total, nil
}

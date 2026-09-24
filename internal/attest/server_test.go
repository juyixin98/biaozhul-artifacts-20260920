package attest

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHTTPServerEndToEnd(t *testing.T) {
	e := newTestEnv(t)
	srv := httptest.NewServer(NewHandler(e.verif))
	defer srv.Close()

	// 健康检查。
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("healthz 状态码 %d", resp.StatusCode)
	}

	// 合法证明 → 200 accepted。
	env := e.sign(t, e.validAttestation())
	res := postVerify(t, srv.URL, env)
	if !res.Accepted {
		t.Fatalf("合法证明被拒绝: %s", res.Reason)
	}

	// 重放 → 422。
	resp2, err := http.Post(srv.URL+"/v1/verify", "application/json", bytes.NewReader(env))
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("重放应返回 422，实际 %d", resp2.StatusCode)
	}
	var rej Result
	if err := json.NewDecoder(resp2.Body).Decode(&rej); err != nil {
		t.Fatal(err)
	}
	if rej.Accepted {
		t.Fatalf("重放应被拒绝")
	}
}

func postVerify(t *testing.T, base string, body []byte) Result {
	t.Helper()
	resp, err := http.Post(base+"/v1/verify", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var res Result
	if err := json.NewDecoder(resp.Body).Decode(&res); err != nil {
		t.Fatal(err)
	}
	return res
}

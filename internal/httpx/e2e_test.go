package httpx_test

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/labstack/echo/v4"

	"synapticgo/internal/dataset"
	"synapticgo/internal/experiments"
	"synapticgo/internal/httpx"
	"synapticgo/internal/inference"
	"synapticgo/internal/modelx"
	"synapticgo/internal/testutil"
)

type client struct {
	t   *testing.T
	e   *echo.Echo
	key string
}

func (c *client) do(method, path string, body []byte, digestHdr string) (int, map[string]any, []byte) {
	c.t.Helper()
	req := httptest.NewRequest(method, path, bytes.NewReader(body))
	if body != nil {
		if digestHdr != "" {
			req.Header.Set(echo.HeaderContentType, "application/octet-stream")
		} else {
			req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
		}
	}
	if digestHdr != "" {
		req.Header.Set("Digest", "sha-256="+digestHdr)
	}
	if c.key != "" {
		req.Header.Set(echo.HeaderAuthorization, "Bearer "+c.key)
	}
	rec := httptest.NewRecorder()
	c.e.ServeHTTP(rec, req)
	raw := rec.Body.Bytes()
	var parsed map[string]any
	_ = json.Unmarshal(raw, &parsed)
	return rec.Code, parsed, raw
}

func (c *client) json(path string, v any) map[string]any {
	c.t.Helper()
	b, _ := json.Marshal(v)
	_, m, _ := c.do(http.MethodPost, path, b, "")
	return m
}

func setup(t *testing.T) (*testutil.Env, *echo.Echo) {
	env := testutil.New(t)
	deps := httpx.Deps{
		DB:          env.DB,
		Datasets:    dataset.NewService(env.DB, env.Objects, env.Store),
		Models:      modelx.NewService(env.DB),
		Experiments: experiments.NewService(env.DB),
	}
	return env, httpx.NewRouter(deps)
}

func sha(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

func TestCreateUserBootstrapReturnsKeyOnce(t *testing.T) {
	_, e := setup(t)
	c := &client{t: t, e: e}
	m := c.json("/api/v1/users", map[string]any{"username": "dora"})
	key, ok := m["api_key"].(string)
	if !ok || key == "" {
		t.Fatalf("no api key: %v", m)
	}
	// The key authenticates.
	auth := &client{t: t, e: e, key: key}
	code, _, _ := auth.do(http.MethodGet, "/api/v1/datasets", nil, "")
	if code != http.StatusOK {
		t.Fatalf("authenticated list = %d", code)
	}
	// No key -> 401.
	code, _, _ = c.do(http.MethodGet, "/api/v1/datasets", nil, "")
	if code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated = %d want 401", code)
	}
}

func TestFullUploadPublishInferOverHTTP(t *testing.T) {
	env, e := setup(t)
	c := &client{t: t, e: e}
	m := c.json("/api/v1/users", map[string]any{"username": "erin"})
	c.key = m["api_key"].(string)

	// Register bob too (isolation).
	bob := &client{t: t, e: e}
	bm := bob.json("/api/v1/users", map[string]any{"username": "finn"})
	bob.key = bm["api_key"].(string)

	data := []byte("HTTP end-to-end dataset payload, chunked and verified 987654")
	whole := sha(data)
	cs := 16
	dm := c.json("/api/v1/datasets", map[string]any{
		"name": "ds", "total_size": len(data), "chunk_size": cs, "whole_digest": whole,
	})
	dsID := int64(dm["id"].(float64))

	// Upload out of order: last chunk first, then the rest ascending.
	n := (len(data) + cs - 1) / cs
	order := []int{n - 1}
	for j := 0; j < n-1; j++ {
		order = append(order, j)
	}
	for _, j := range order {
		chunk := data[j*cs:]
		if len(chunk) > cs {
			chunk = chunk[:cs]
		}
		code, rm, _ := c.do(http.MethodPut,
			fmt.Sprintf("/api/v1/datasets/%d/chunks/%d", dsID, j), chunk, sha(chunk))
		if code != http.StatusCreated {
			t.Fatalf("chunk %d -> %d: %v", j, code, rm)
		}
	}

	// Publish.
	code, pm, _ := c.do(http.MethodPost,
		fmt.Sprintf("/api/v1/datasets/%d/publish", dsID), []byte("{}"), "")
	if code != http.StatusOK {
		t.Fatalf("publish = %d: %v", code, pm)
	}
	if pm["whole_digest"] != whole {
		t.Fatal("whole digest mismatch over HTTP")
	}

	// Bob cannot see or publish/delete Alice's dataset.
	if code, _, _ := bob.do(http.MethodGet, fmt.Sprintf("/api/v1/datasets/%d", dsID), nil, ""); code != http.StatusForbidden {
		t.Fatalf("cross-user get = %d want 403", code)
	}

	// Register a 2-class model.
	w := &inference.Weights{InputDim: 3, NumClasses: 2,
		W: []float32{1, -1, 0.5, 0, 1, -0.5}, B: []float32{-0.25, 0.25}}
	mm := c.json("/api/v1/models", map[string]any{
		"model_name": "clf", "dataset_id": dsID, "input_dim": 3,
		"classes":        []string{"cat", "dog"},
		"weights_base64": base64.StdEncoding.EncodeToString(w.Encode()),
	})
	modelID, ok := mm["id"].(float64)
	if !ok {
		t.Fatalf("model register: %v", mm)
	}

	// Predict and verify numeric result: x=[2,1,0] -> dog ~0.62246.
	code, pr, _ := c.do(http.MethodPost,
		fmt.Sprintf("/api/v1/models/%d/predict", int64(modelID)),
		[]byte(`{"features":[2,1,0]}`), "")
	if code != http.StatusOK {
		t.Fatalf("predict = %d: %v", code, pr)
	}
	if pr["predicted_class"] != "dog" {
		t.Fatalf("class = %v want dog", pr["predicted_class"])
	}
	conf := pr["confidence"].(float64)
	if d := conf - 0.6224593312; d > 1e-5 || d < -1e-5 {
		t.Fatalf("confidence = %v", conf)
	}

	// Bad shape rejected.
	code, br, _ := c.do(http.MethodPost,
		fmt.Sprintf("/api/v1/models/%d/predict", int64(modelID)),
		[]byte(`{"features":[1,2]}`), "")
	if code != http.StatusUnprocessableEntity {
		t.Fatalf("shape error = %d: %v", code, br)
	}

	// Experiment recorded and listed.
	code, lr, _ := c.do(http.MethodGet,
		fmt.Sprintf("/api/v1/models/%d/experiments", int64(modelID)), nil, "")
	if code != http.StatusOK {
		t.Fatalf("experiments = %d", code)
	}
	items := lr["items"].([]any)
	if len(items) != 1 {
		t.Fatalf("expected 1 experiment record, got %d", len(items))
	}

	// Deleting the referenced dataset fails; removing experiments then model is
	// enforced (model delete 409 here).
	code, _, _ = c.do(http.MethodDelete, fmt.Sprintf("/api/v1/datasets/%d", dsID), nil, "")
	if code != http.StatusConflict {
		t.Fatalf("referenced dataset delete = %d want 409", code)
	}
	code, _, _ = c.do(http.MethodDelete, fmt.Sprintf("/api/v1/models/%d", int64(modelID)), nil, "")
	if code != http.StatusConflict {
		t.Fatalf("model with records delete = %d want 409", code)
	}
	_ = env
}

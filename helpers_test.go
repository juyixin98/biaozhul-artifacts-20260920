package synapticgo_test

import (
	"encoding/json"
	"sync"
	"testing"
)

// BlobPathForTest exposes the content-addressed path of a blob hash.
func (e *testEnv) BlobPathForTest(sha string) string {
	return e.Store.BlobPath(sha)
}

var (
	tokenCache   = map[string]string{}
	tokenCacheMu sync.Mutex
)

// registerCachedUser creates a user and remembers the token by name.
func (e *testEnv) registerCachedUser(name string) *client {
	c := e.registerUser(name)
	tokenCacheMu.Lock()
	tokenCache[name] = c.token
	tokenCacheMu.Unlock()
	return c
}

func tokenFor(t *testing.T, env *testEnv, name string) string {
	t.Helper()
	tokenCacheMu.Lock()
	tok, ok := tokenCache[name]
	tokenCacheMu.Unlock()
	if !ok {
		t.Fatalf("no cached token for %q", name)
	}
	return tok
}

// datasetJSON2D builds a small valid 2-dimensional labelled dataset document.
func datasetJSON2D() []byte {
	type ex struct {
		X     []float64 `json:"x"`
		Label int       `json:"label"`
	}
	doc := map[string]any{
		"dim": 2,
		"examples": []ex{
			{X: []float64{0, 0}, Label: 0},
			{X: []float64{1, 1}, Label: 1},
			{X: []float64{0.5, 0.5}, Label: 0},
		},
	}
	b, _ := json.Marshal(doc)
	return b
}

// registerLinearModel creates a model via the API and returns the view.
func registerLinearModel(t *testing.T, c *client, name, version string, dim int,
	classes []string, trainID, evalID *int64, metrics map[string]float64) map[string]any {
	t.Helper()
	k := len(classes)
	W := make([][]float64, k)
	b := make([]float64, k)
	for i := range W {
		W[i] = make([]float64, dim)
		for j := range W[i] {
			W[i][j] = float64(i+1) * 0.25
		}
		b[i] = -0.1 * float64(i)
	}
	weights := map[string]any{
		"type": "linear_softmax", "version": 1, "dim": dim,
		"classes": classes, "W": W, "b": b,
	}
	req := map[string]any{
		"name": name, "version": version, "weights": weights,
	}
	if trainID != nil {
		req["train_dataset_id"] = *trainID
	}
	if evalID != nil {
		req["eval_dataset_id"] = *evalID
	}
	if metrics != nil {
		req["metrics"] = metrics
	}
	code, body := c.doJSON("POST", "/v1/models", req, nil)
	if code != 201 {
		t.Fatalf("register model %s/%s: %d %s", name, version, code, body)
	}
	var v map[string]any
	mustJSON(body, &v)
	return v
}

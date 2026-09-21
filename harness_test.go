package synapticgo_test

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jmoiron/sqlx"
	"github.com/labstack/echo/v4"

	"github.com/synapticgo/synapticgo/internal/api"
	"github.com/synapticgo/synapticgo/internal/config"
	"github.com/synapticgo/synapticgo/internal/database"
	"github.com/synapticgo/synapticgo/internal/dataset"
	model "github.com/synapticgo/synapticgo/internal/model"
	"github.com/synapticgo/synapticgo/internal/storage"
	"github.com/synapticgo/synapticgo/internal/user"
)

// testEnv is a full backend instance on a throw-away database.
type testEnv struct {
	t        *testing.T
	adminDB  *sqlx.DB // connection to the "postgres" admin database
	dbName   string
	DB       *sqlx.DB
	Store    *storage.Store
	DataDir  string
	Datasets *dataset.Service
	Models   *model.Service
	Echo     *echo.Echo
	Server   *httptest.Server
}

func testDatabaseURL() string {
	if v := os.Getenv("SYN_TEST_DATABASE_URL"); v != "" {
		return v
	}
	return "postgres://synapticgo:synapticgo@localhost:5432/synapticgo_test?sslmode=disable"
}

// adminURL derives a connection to the "postgres" maintenance database from
// the configured test URL so we can create/drop throw-away databases.
func adminURL() (string, string) {
	raw := testDatabaseURL()
	u, err := url.Parse(raw)
	if err != nil {
		return raw, ""
	}
	testDB := strings.TrimPrefix(u.Path, "/")
	u.Path = "/postgres"
	return u.String(), testDB
}

func newTestEnv(t *testing.T) *testEnv {
	t.Helper()
	if os.Getenv("SYN_TEST_DATABASE_URL") == "" {
		// default probe
	}
	admin, err := sqlx.Connect("postgres", testDatabaseURL())
	if err != nil {
		t.Skipf("test database unavailable (%v); set SYN_TEST_DATABASE_URL", err)
	}
	admin.Close()

	adminURLStr, baseName := adminURL()
	adminDB, err := sqlx.Connect("postgres", adminURLStr)
	if err != nil {
		t.Skipf("admin database unavailable (%v)", err)
	}

	buf := make([]byte, 6)
	_, _ = rand.Read(buf)
	name := fmt.Sprintf("%s_%s", baseName, hex.EncodeToString(buf))
	name = strings.ReplaceAll(name, "-", "_")
	if len(name) > 40 {
		name = name[:40]
	}
	if _, err := adminDB.Exec("CREATE DATABASE " + name); err != nil {
		adminDB.Close()
		t.Fatalf("create test database: %v", err)
	}
	t.Cleanup(func() {
		adminDB.Exec("DROP DATABASE IF EXISTS " + name)
		adminDB.Close()
	})

	u, _ := url.Parse(adminURLStr)
	u.Path = "/" + name
	db, err := database.Connect(u.String())
	if err != nil {
		t.Fatalf("connect %s: %v", name, err)
	}
	t.Cleanup(func() { db.Close() })
	if err := database.Migrate(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	dataDir := t.TempDir()
	store, err := storage.New(dataDir)
	if err != nil {
		t.Fatalf("storage: %v", err)
	}
	datasetSvc := &dataset.Service{DB: db, Store: store}
	if err := datasetSvc.RecoverAtStartup(context.Background()); err != nil {
		t.Fatalf("recover: %v", err)
	}
	modelSvc := &model.Service{DB: db, Datasets: datasetSvc}
	userSvc := &user.Service{DB: db}

	cfg := config.Config{HTTPAddr: "", MaxChunkBytes: 64 << 20, MaxJSONBytes: 8 << 20}
	e := api.NewServer(api.Deps{
		Users: userSvc, Datasets: datasetSvc, Models: modelSvc,
		Cfg: cfg,
	})
	srv := httptest.NewServer(e)
	t.Cleanup(srv.Close)

	return &testEnv{
		t: t, adminDB: adminDB, dbName: name, DB: db, Store: store,
		DataDir: dataDir, Datasets: datasetSvc, Models: modelSvc,
		Echo: e, Server: srv,
	}
}

// client is an authenticated JSON HTTP client.
type client struct {
	env   *testEnv
	token string
}

func (e *testEnv) registerUser(name string) *client {
	e.t.Helper()
	code, body := e.do("POST", "/v1/users", "", map[string]any{"name": name})
	if code != 201 {
		e.t.Fatalf("register user %s: %d %s", name, code, body)
	}
	var resp struct {
		Token string `json:"token"`
	}
	mustJSON(body, &resp)
	return &client{env: e, token: resp.Token}
}

func (e *testEnv) do(method, path, token string, payload any) (int, []byte) {
	var r io.Reader
	if payload != nil {
		switch v := payload.(type) {
		case []byte:
			r = bytes.NewReader(v)
		case io.Reader:
			r = v
		default:
			b, _ := json.Marshal(v)
			r = bytes.NewReader(b)
		}
	}
	req := httptest.NewRequest(method, e.Server.URL+path, r)
	if payload != nil {
		if _, ok := payload.(io.Reader); ok || isBytes(payload) {
			req.Header.Set("Content-Type", "application/octet-stream")
		} else {
			req.Header.Set("Content-Type", "application/json")
		}
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	e.Echo.ServeHTTP(rec, req)
	raw, _ := io.ReadAll(rec.Result().Body)
	return rec.Code, raw
}

func isBytes(v any) bool {
	_, ok := v.([]byte)
	return ok
}

func (c *client) doJSON(method, path string, payload any, out any) (int, []byte) {
	code, body := c.env.do(method, path, c.token, payload)
	if out != nil && code < 300 {
		mustJSON(body, out)
	}
	return code, body
}

func (c *client) doRaw(method, path string, body []byte) (int, []byte) {
	return c.env.do(method, path, c.token, body)
}

func mustJSON(b []byte, v any) {
	if err := json.Unmarshal(b, v); err != nil {
		panic(fmt.Sprintf("invalid JSON response %q: %v", string(b), err))
	}
}

// chunkPlan describes a deterministic split of content for upload.
type chunkPlan struct {
	content   []byte
	chunkSize int64
	chunks    [][]byte
	wholeSHA  string
}

func makeChunks(content []byte, chunkSize int64) *chunkPlan {
	if chunkSize <= 0 {
		chunkSize = int64(len(content))
	}
	p := &chunkPlan{content: content, chunkSize: chunkSize}
	for off := 0; off < len(content); off += int(chunkSize) {
		end := off + int(chunkSize)
		if end > len(content) {
			end = len(content)
		}
		p.chunks = append(p.chunks, content[off:end])
	}
	sum := sha256.Sum256(content)
	p.wholeSHA = hex.EncodeToString(sum[:])
	return p
}

func shaHex(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

type chunkSpecDTO struct {
	Idx    int    `json:"idx"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
}

// createDataset registers a manifest for the plan.
func (c *client) createDataset(name string, p *chunkPlan, overrideWhole string) map[string]any {
	specs := make([]chunkSpecDTO, len(p.chunks))
	for i, ch := range p.chunks {
		specs[i] = chunkSpecDTO{Idx: i, Size: int64(len(ch)), SHA256: shaHex(ch)}
	}
	whole := p.wholeSHA
	if overrideWhole != "" {
		whole = overrideWhole
	}
	req := map[string]any{
		"name":         name,
		"total_size":   len(p.content),
		"chunk_size":   p.chunkSize,
		"whole_sha256": whole,
		"chunks":       specs,
	}
	code, body := c.doJSON("POST", "/v1/datasets", req, nil)
	if code != 201 {
		c.env.t.Fatalf("create dataset: %d %s", code, body)
	}
	var v map[string]any
	mustJSON(body, &v)
	return v
}

func (c *client) uploadChunk(datasetID int, idx int, body []byte) (int, []byte) {
	return c.doRaw("PUT", fmt.Sprintf("/v1/datasets/%d/chunks/%d", datasetID, idx), body)
}

func (c *client) publish(datasetID int) (int, map[string]any) {
	code, body := c.doJSON("POST", fmt.Sprintf("/v1/datasets/%d/publish", datasetID), nil, nil)
	var v map[string]any
	if code < 300 {
		mustJSON(body, &v)
	}
	return code, v
}

func datasetID(v map[string]any) int { return int(v["id"].(float64)) }

// eventually retries f until it returns nil or the timeout elapses.
func eventually(t *testing.T, d time.Duration, f func() error) {
	t.Helper()
	deadline := time.Now().Add(d)
	var lastErr error
	for time.Now().Before(deadline) {
		if lastErr = f(); lastErr == nil {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("condition not met within %s: %v", d, lastErr)
}

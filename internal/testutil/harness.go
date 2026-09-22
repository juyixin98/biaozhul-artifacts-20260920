// Package testutil provides an isolated PostgreSQL-backed harness for
// integration tests. Set RENDERQ_TEST_DATABASE_URL to a superuser URL
// (e.g. postgres://postgres@localhost:5432/postgres?sslmode=disable);
// each Harness creates a fresh, uniquely named database and skips the test
// when no database is reachable.
package testutil

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/vfxqueue/renderq/internal/api"
	"github.com/vfxqueue/renderq/internal/db/dbgen"
	"github.com/vfxqueue/renderq/internal/storage"
	"github.com/vfxqueue/renderq/migrations"
)

// AdminKey / member keys provisioned by every harness.
const (
	AdminKey = "test-admin-key"
	AliceKey = "test-alice-key"
	BobKey   = "test-bob-key"
)

type Harness struct {
	T        *testing.T
	Ctx      context.Context
	AdminURL string
	Pool     *pgxpool.Pool
	Store    *storage.Store
	Server   *httptest.Server
	DataDir  string
	baseURL  string
}

// New provisions a throwaway database and returns a running HTTP server.
func New(t *testing.T) *Harness {
	t.Helper()
	adminURL := os.Getenv("RENDERQ_TEST_DATABASE_URL")
	if adminURL == "" {
		adminURL = "postgres://renderq:renderq@localhost:5432/postgres?sslmode=disable"
	}

	name := "rqtest_" + strings.ReplaceAll(uuid.NewString()[:12], "-", "")
	bootstrap(t, adminURL, "CREATE DATABASE "+name)

	// Rebuild URL to point at the new database.
	cfg, err := pgx.ParseConfig(adminURL)
	if err != nil {
		t.Fatalf("parse url: %v", err)
	}
	cfg.Database = name
	conn, err := pgx.ConnectConfig(context.Background(), cfg)
	if err != nil {
		t.Fatalf("connect test db: %v", err)
	}
	if err := migrations.Migrate(context.Background(), conn); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	_ = conn.Close(context.Background())

	poolCfg, err := pgxpool.ParseConfig(adminURL)
	if err != nil {
		t.Fatalf("pool parse: %v", err)
	}
	poolCfg.ConnConfig.Database = name
	poolCfg.MaxConns = 8
	pool, err := pgxpool.NewWithConfig(context.Background(), poolCfg)
	if err != nil {
		t.Fatalf("pool: %v", err)
	}
	t.Cleanup(func() {
		pool.Close()
		bootstrap(t, adminURL, "DROP DATABASE "+name)
	})

	dir, err := os.MkdirTemp("", "renderq-test-*")
	if err != nil {
		t.Fatalf("tmpdir: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	store, err := storage.New(dir)
	if err != nil {
		t.Fatalf("storage: %v", err)
	}

	q := dbgen.New(pool)
	mkUser := func(username, role, key string) {
		if _, err := q.CreateUser(context.Background(), dbgen.CreateUserParams{
			Username: username, Role: role, ApiKeyHash: api.HashAPIKey(key),
		}); err != nil {
			t.Fatalf("create user %s: %v", username, err)
		}
	}
	mkUser("admin", "admin", AdminKey)
	mkUser("alice", "member", AliceKey)
	mkUser("bob", "member", BobKey)

	srv := api.NewServer(pool, store)
	ts := httptest.NewServer(srv.Router())
	t.Cleanup(ts.Close)

	return &Harness{
		T: t, Ctx: context.Background(), AdminURL: adminURL,
		Pool: pool, Store: store, Server: ts, DataDir: dir,
		baseURL: ts.URL,
	}
}

func bootstrap(t *testing.T, url, stmt string) {
	t.Helper()
	deadline := time.Now().Add(30 * time.Second)
	var lastErr error
	for time.Now().Before(deadline) {
		conn, err := pgx.Connect(context.Background(), url)
		if err == nil {
			_, err = conn.Exec(context.Background(), stmt)
			_ = conn.Close(context.Background())
			if err != nil {
				lastErr = err
			} else {
				return
			}
		} else {
			lastErr = err
		}
		time.Sleep(300 * time.Millisecond)
	}
	t.Skipf("test database unavailable (%v); set RENDERQ_TEST_DATABASE_URL", lastErr)
}

// ---------------------------------------------------------------------------
// JSON HTTP helpers
// ---------------------------------------------------------------------------

func (h *Harness) Do(method, path, key string, body any) (int, map[string]any) {
	h.T.Helper()
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, _ := http.NewRequestWithContext(h.Ctx, method, h.baseURL+path, rdr)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if key != "" {
		req.Header.Set("X-API-Key", key)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		h.T.Fatalf("http: %v", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out map[string]any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	if out == nil {
		out = map[string]any{}
	}
	return resp.StatusCode, out
}

// DoRaw posts an arbitrary body and returns status + raw bytes.
func (h *Harness) DoRaw(method, path, key, contentType string, body []byte) (int, []byte) {
	h.T.Helper()
	req, _ := http.NewRequestWithContext(h.Ctx, method, h.baseURL+path, bytes.NewReader(body))
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	if key != "" {
		req.Header.Set("X-API-Key", key)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		h.T.Fatalf("http: %v", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, raw
}

func asID(m map[string]any) uuid.UUID {
	id, err := uuid.Parse(m["id"].(string))
	if err != nil {
		panic(fmt.Sprintf("missing id in %v", m))
	}
	return id
}

// AsID extracts a uuid field from a response body.
func (h *Harness) AsID(m map[string]any, field string) uuid.UUID {
	h.T.Helper()
	s, ok := m[field].(string)
	if !ok {
		h.T.Fatalf("field %q missing in %v", field, m)
	}
	id, err := uuid.Parse(s)
	if err != nil {
		h.T.Fatalf("field %q not uuid: %v", field, m)
	}
	return id
}

// BaseURL returns the test server base URL.
func (h *Harness) BaseURL() string { return h.baseURL }

// ---------------------------------------------------------------------------
// Domain seeding helpers
// ---------------------------------------------------------------------------

func (h *Harness) CreateProject(key, name string) uuid.UUID {
	h.T.Helper()
	st, body := h.Do("POST", "/projects", key, map[string]string{"name": name})
	if st != http.StatusCreated {
		h.T.Fatalf("create project: %d %v", st, body)
	}
	return asID(body)
}

// MakePNG builds a w*h PNG. Every pixel is the supplied straight-alpha
// color except a transparent border.
func MakePNG(w, h int, c color.RGBA) []byte {
	img := image.NewNRGBA(image.Rect(0, 0, w, h))
	nc := color.NRGBA{R: c.R, G: c.G, B: c.B, A: c.A}
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			if x == 0 || y == 0 || x == w-1 || y == h-1 {
				img.SetNRGBA(x, y, color.NRGBA{0, 0, 0, 0})
			} else {
				img.SetNRGBA(x, y, nc)
			}
		}
	}
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		panic(err)
	}
	return buf.Bytes()
}

// UploadAsset uploads a PNG as multipart form data.
func (h *Harness) UploadAsset(key string, projectID uuid.UUID, relPath string, pngBytes []byte) map[string]any {
	h.T.Helper()
	var body bytes.Buffer
	mw := multipart.NewWriter(&body)
	_ = mw.WriteField("path", relPath)
	part, err := mw.CreateFormFile("file", relPath)
	if err != nil {
		h.T.Fatal(err)
	}
	if _, err := part.Write(pngBytes); err != nil {
		h.T.Fatal(err)
	}
	_ = mw.Close()

	req, _ := http.NewRequestWithContext(h.Ctx, "POST",
		fmt.Sprintf("%s/projects/%s/assets", h.baseURL, projectID), &body)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	req.Header.Set("X-API-Key", key)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		h.T.Fatalf("upload: %v", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusCreated {
		h.T.Fatalf("upload asset %s: %d %s", relPath, resp.StatusCode, raw)
	}
	var out map[string]any
	_ = json.Unmarshal(raw, &out)
	return out
}

// FreezeManifest posts a manifest and returns the version body.
func (h *Harness) FreezeManifest(key string, compID uuid.UUID, manifest any) (int, map[string]any) {
	h.T.Helper()
	return h.Do("POST", "/compositions/"+compID.String()+"/versions", key,
		map[string]any{"manifest": manifest})
}

// WaitForJobStatus polls a job until it reaches one of the terminal states
// (or want) or times out.
func (h *Harness) WaitForJobStatus(jobID uuid.UUID, want ...string) map[string]any {
	h.T.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		st, body := h.Do("GET", "/jobs/"+jobID.String(), AdminKey, nil)
		if st != http.StatusOK {
			h.T.Fatalf("get job: %d %v", st, body)
		}
		status := body["status"].(string)
		for _, w := range want {
			if status == w {
				return body
			}
		}
		if status == "succeeded" || status == "failed" || status == "canceled" {
			// Terminal but not the wanted state: keep looping briefly only
			// if caller explicitly waits for something else; return anyway.
			return body
		}
		time.Sleep(50 * time.Millisecond)
	}
	h.T.Fatalf("job %s did not reach %v within timeout", jobID, want)
	return nil
}

// MustParseID is a small helper.
func MustParseID(s string) uuid.UUID {
	id, err := uuid.Parse(s)
	if err != nil {
		panic(err)
	}
	return id
}

// Package testsupport provisions a real PostgreSQL for integration tests.
// It uses TEST_DATABASE_URL when provided, otherwise starts an ephemeral
// postgres:16 container with docker and destroys it at process exit.
package testsupport

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"vfxqueue/internal/compositor"
	"vfxqueue/internal/config"
	"vfxqueue/internal/db/gen"
	"vfxqueue/internal/idgen"
	"vfxqueue/internal/migrate"
	"vfxqueue/internal/storage"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	sharedOnce sync.Once
	sharedDB   string
	sharedStop func()
	sharedErr  error
)

func freePort(t testing.TB) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := l.Addr().(*net.TCPAddr).Port
	_ = l.Close()
	return port
}

func startPostgres(ctx context.Context, t testing.TB) (string, func()) {
	if u := os.Getenv("TEST_DATABASE_URL"); u != "" {
		return u, func() {}
	}
	if _, err := exec.LookPath("docker"); err != nil {
		t.Skip("docker not available and TEST_DATABASE_URL unset; skipping integration test")
	}
	port := freePort(t)
	run := exec.CommandContext(ctx, "docker", "run", "-d", "--rm",
		"-e", "POSTGRES_USER=vfxtest",
		"-e", "POSTGRES_PASSWORD=vfxtest",
		"-e", "POSTGRES_DB=vfxtest",
		"-p", fmt.Sprintf("127.0.0.1:%d:5432", port),
		"postgres:16-alpine")
	var out bytes.Buffer
	run.Stdout = &out
	run.Stderr = &out
	if err := run.Run(); err != nil {
		t.Fatalf("docker run: %v\n%s", err, out.String())
	}
	cid := strings.TrimSpace(out.String())
	url := fmt.Sprintf("postgres://vfxtest:vfxtest@127.0.0.1:%d/vfxtest?sslmode=disable", port)

	ready, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	for ready.Err() == nil {
		c, err := pgx.Connect(ready, url)
		if err == nil {
			_ = c.Close(ready)
			break
		}
		time.Sleep(300 * time.Millisecond)
	}
	if ready.Err() != nil {
		_ = exec.Command("docker", "kill", cid).Run()
		t.Fatalf("postgres did not become ready: %v", ready.Err())
	}
	return url, func() { _ = exec.Command("docker", "kill", cid).Run() }
}

// Harness is a clean schema + temp data dir for one test function.
type Harness struct {
	T    *testing.T
	Ctx  context.Context
	URL  string
	Pool *pgxpool.Pool
	Q    *gen.Queries
	Dir  string
}

func setupShared(t testing.TB) {
	sharedOnce.Do(func() {
		ctx := context.Background()
		url, stop := startPostgres(ctx, t)
		conn, err := pgx.Connect(ctx, url)
		if err != nil {
			sharedErr = err
			stop()
			return
		}
		if err := migrate.Run(ctx, conn); err != nil {
			sharedErr = err
			_ = conn.Close(ctx)
			stop()
			return
		}
		_ = conn.Close(ctx)
		sharedDB, sharedStop = url, stop
	})
	if sharedErr != nil {
		t.Fatal(sharedErr)
	}
}

// New returns a harness with all tables truncated and sequences reset.
func New(t *testing.T) *Harness {
	t.Helper()
	setupShared(t)
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, sharedDB)
	if err != nil {
		t.Fatal(err)
	}
	h := &Harness{
		T: t, Ctx: ctx, URL: sharedDB, Pool: pool,
		Q:   gen.New(pool),
		Dir: t.TempDir(),
	}
	h.truncate()
	t.Cleanup(func() { pool.Close() })
	return h
}

func (h *Harness) truncate() {
	_, err := h.Pool.Exec(h.Ctx, `
		TRUNCATE TABLE composition_version_resources, composition_versions,
		             compositions, frames, render_tasks, assets,
		             project_members, projects, users
		CASCADE`)
	if err != nil {
		h.T.Fatalf("truncate: %v", err)
	}
	// enqueued_seq drives FIFO ordering; reset it so each test starts at 1.
	_, _ = h.Pool.Exec(h.Ctx, `ALTER SEQUENCE render_tasks_enqueued_seq_seq RESTART WITH 1`)
}

func (h *Harness) AssetsDir() string  { return filepath.Join(h.Dir, "assets") }
func (h *Harness) OutputsDir() string { return filepath.Join(h.Dir, "outputs") }

// Config builds a worker config tuned for fast tests; callers may tweak.
func (h *Harness) Config() config.Config {
	return config.Config{
		DatabaseURL:       h.URL,
		DataDir:           h.Dir,
		AssetsDir:         h.AssetsDir(),
		OutputsDir:        h.OutputsDir(),
		WorkerConcurrency: 2,
		LeaseTTL:          500 * time.Millisecond,
		HeartbeatInterval: 150 * time.Millisecond,
		PollInterval:      20 * time.Millisecond,
	}
}

// ---- fixtures ----

func (h *Harness) CreateUser(role string) gen.User {
	u, err := h.Q.CreateUser(h.Ctx, gen.CreateUserParams{
		ID:          idgen.NewID(),
		Email:       "u" + idgen.NewID().String() + "@example.com",
		DisplayName: "Test " + role,
		Role:        role,
		ApiToken:    "tok-" + idgen.NewID().String(),
	})
	if err != nil {
		h.T.Fatal(err)
	}
	return u
}

func (h *Harness) CreateProject(owner gen.User) gen.Project {
	p, err := h.Q.CreateProject(h.Ctx, gen.CreateProjectParams{
		ID: idgen.NewID(), Name: "p", CreatedBy: owner.ID,
	})
	if err != nil {
		h.T.Fatal(err)
	}
	if err := h.Q.AddProjectMember(h.Ctx, gen.AddProjectMemberParams{
		ProjectID: p.ID, UserID: owner.ID, Role: "owner",
	}); err != nil {
		h.T.Fatal(err)
	}
	return p
}

// AddMember gives user a membership.
func (h *Harness) AddMember(p gen.Project, u gen.User, role string) {
	if err := h.Q.AddProjectMember(h.Ctx, gen.AddProjectMemberParams{
		ProjectID: p.ID, UserID: u.ID, Role: role,
	}); err != nil {
		h.T.Fatal(err)
	}
}

// SolidPNG serializes a solid color rectangle and stores it on disk like the
// upload endpoint does.
func SolidPNG(w, hh int, c color.NRGBA) []byte {
	img := image.NewNRGBA(image.Rect(0, 0, w, hh))
	b := img.Bounds()
	for y := b.Min.Y; y < b.Max.Y; y++ {
		for x := b.Min.X; x < b.Max.X; x++ {
			img.SetNRGBA(x, y, c)
		}
	}
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		panic(err)
	}
	return buf.Bytes()
}

// CreateAsset writes data into content-addressed storage and inserts the row.
func (h *Harness) CreateAsset(p gen.Project, uploader gen.User, filename string, data []byte) gen.Asset {
	sum := sha256.Sum256(data)
	hexSum := hex.EncodeToString(sum[:])
	path := storage.AssetPath(h.AssetsDir(), hexSum)
	if err := storage.AtomicWriteFile(path, data); err != nil {
		h.T.Fatal(err)
	}
	cfg, err := png.DecodeConfig(bytes.NewReader(data))
	if err != nil {
		h.T.Fatal(err)
	}
	a, err := h.Q.CreateAsset(h.Ctx, gen.CreateAssetParams{
		ID: idgen.NewID(), ProjectID: p.ID, Filename: filename,
		ContentType: "image/png", Width: int32(cfg.Width), Height: int32(cfg.Height),
		SizeBytes: int64(len(data)), Sha256: hexSum, StoragePath: path,
		UploadedBy: uploader.ID,
	})
	if err != nil {
		h.T.Fatal(err)
	}
	return a
}

// FreezeVersion creates a composition and immutable version 1 from spec.
func (h *Harness) FreezeVersion(p gen.Project, owner gen.User, spec *compositor.Spec) (gen.Composition, gen.CompositionVersion) {
	comp, err := h.Q.CreateComposition(h.Ctx, gen.CreateCompositionParams{
		ID: idgen.NewID(), ProjectID: p.ID, Name: "c",
		CanvasWidth: int32(spec.CanvasWidth), CanvasHeight: int32(spec.CanvasHeight),
		FrameCount: int32(spec.FrameCount), CreatedBy: owner.ID,
	})
	if err != nil {
		h.T.Fatal(err)
	}
	ver := h.freezeVersion(comp, owner, spec, 1)
	return comp, ver
}

// FreezeVersionNumbered freezes an additional version of an existing
// composition with an explicit version number.
func (h *Harness) FreezeVersionNumbered(p gen.Project, owner gen.User, comp gen.Composition, number int, spec *compositor.Spec) (gen.Composition, gen.CompositionVersion) {
	ver := h.freezeVersion(comp, owner, spec, int32(number))
	vID := ver.ID
	if err := h.Q.SetCompositionCurrentVersion(h.Ctx, gen.SetCompositionCurrentVersionParams{
		ID: comp.ID, CurrentVersionID: &vID,
	}); err != nil {
		h.T.Fatal(err)
	}
	return comp, ver
}

func (h *Harness) freezeVersion(comp gen.Composition, owner gen.User, spec *compositor.Spec, number int32) gen.CompositionVersion {
	canon, err := compositor.CanonicalJSON(spec)
	if err != nil {
		h.T.Fatal(err)
	}
	sum := sha256.Sum256(canon)
	ver, err := h.Q.CreateCompositionVersion(h.Ctx, gen.CreateCompositionVersionParams{
		ID: idgen.NewID(), CompositionID: comp.ID, VersionNumber: number,
		SpecJson: canon, SpecSha256: hex.EncodeToString(sum[:]), CreatedBy: owner.ID,
	})
	if err != nil {
		h.T.Fatal(err)
	}
	for _, l := range spec.Layers {
		a, err := h.Q.GetAssetByID(h.Ctx, mustUUID(h.T, l.AssetID))
		if err != nil {
			h.T.Fatal(err)
		}
		if err := h.Q.AddVersionResource(h.Ctx, gen.AddVersionResourceParams{
			VersionID: ver.ID, LayerID: l.ID, AssetID: a.ID, AssetSha256: a.Sha256,
		}); err != nil {
			h.T.Fatal(err)
		}
	}
	return ver
}

// Enqueue inserts a task and its frames.
func (h *Harness) Enqueue(p gen.Project, comp gen.Composition, ver gen.CompositionVersion,
	start, end, priority int, owner gen.User) gen.RenderTask {
	taskID := idgen.NewID()
	outDir := filepath.Join(h.OutputsDir(), taskID.String())
	if err := storage.EnsureDir(outDir); err != nil {
		h.T.Fatal(err)
	}
	task, err := h.Q.CreateTask(h.Ctx, gen.CreateTaskParams{
		ID: taskID, ProjectID: p.ID, CompositionID: comp.ID, VersionID: ver.ID,
		FrameStart: int32(start), FrameEnd: int32(end), Priority: int16(priority),
		CreatedBy: owner.ID, OutputDir: outDir,
	})
	if err != nil {
		h.T.Fatal(err)
	}
	for f := start; f <= end; f++ {
		if err := h.Q.CreateFrame(h.Ctx, gen.CreateFrameParams{
			ID: idgen.NewID(), TaskID: task.ID, FrameIndex: int32(f),
		}); err != nil {
			h.T.Fatal(err)
		}
	}
	return task
}

func mustUUID(t *testing.T, s string) uuid.UUID {
	t.Helper()
	u, err := uuid.Parse(s)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

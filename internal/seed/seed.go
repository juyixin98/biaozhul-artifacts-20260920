// Package seed creates deterministic demo data (users + project + sample PNG
// assets + a demo composition/version) so `docker compose up` is immediately
// usable. Everything is idempotent.
package seed

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"image"
	"image/color"
	"image/png"

	"vfxqueue/internal/compositor"
	"vfxqueue/internal/db/gen"
	"vfxqueue/internal/idgen"
	"vfxqueue/internal/storage"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Well-known local-only tokens documented in README. Never use in production.
const (
	AdminToken = "dev-admin-token-0001"
	AliceToken = "dev-alice-token-0002"
	BobToken   = "dev-bob-token-0003"
)

type userSeed struct {
	email string
	name  string
	role  string
	token string
}

var users = []userSeed{
	{"admin@example.com", "Ada Admin", "admin", AdminToken},
	{"alice@example.com", "Alice", "member", AliceToken},
	{"bob@example.com", "Bob", "member", BobToken},
}

// Ensure creates demo users, a project owned by Alice, sample PNG assets and
// an initial frozen composition version. Safe to run repeatedly.
func Ensure(ctx context.Context, pool *pgxpool.Pool, q *gen.Queries, assetsDir string) error {
	idByEmail := map[string]uuid.UUID{}
	for _, u := range users {
		row, err := q.GetUserByAPIToken(ctx, u.token)
		if err != nil {
			row, err = q.CreateUser(ctx, gen.CreateUserParams{
				ID: idgen.NewID(), Email: u.email, DisplayName: u.name,
				Role: u.role, ApiToken: u.token,
			})
			if err != nil {
				return fmt.Errorf("create user %s: %w", u.email, err)
			}
		}
		idByEmail[u.email] = row.ID
	}
	alice := idByEmail["alice@example.com"]
	bob := idByEmail["bob@example.com"]

	projID := deterministicUUID("project:demo")
	if _, err := q.GetProject(ctx, projID); err != nil {
		if _, err := q.CreateProject(ctx, gen.CreateProjectParams{
			ID: projID, Name: "Demo project", CreatedBy: alice,
		}); err != nil {
			return fmt.Errorf("create project: %w", err)
		}
	}
	if err := q.AddProjectMember(ctx, gen.AddProjectMemberParams{
		ProjectID: projID, UserID: alice, Role: "owner",
	}); err != nil {
		return err
	}
	if err := q.AddProjectMember(ctx, gen.AddProjectMemberParams{
		ProjectID: projID, UserID: bob, Role: "member",
	}); err != nil {
		return err
	}

	// Three deterministic, real PNGs with alpha.
	type sampleDef struct {
		name    string
		w, h    int
		drawer  func(img *image.NRGBA)
		layerID string
	}
	defs := []sampleDef{
		{"bg", 320, 240, drawBackground, "background"},
		{"circle", 120, 120, drawCircle, "overlay"},
		{"star", 80, 80, drawStar, "badge"},
	}
	layerAssets := map[string]gen.Asset{}
	for _, d := range defs {
		img := image.NewNRGBA(image.Rect(0, 0, d.w, d.h))
		d.drawer(img)
		var buf bytes.Buffer
		if err := png.Encode(&buf, img); err != nil {
			return err
		}
		data := buf.Bytes()
		sum := sha256.Sum256(data)
		hexSum := hex.EncodeToString(sum[:])
		if existing, err := q.GetAssetByProjectHash(ctx, gen.GetAssetByProjectHashParams{
			ProjectID: projID, Sha256: hexSum,
		}); err == nil {
			layerAssets[d.layerID] = existing
			continue
		}
		path := storage.AssetPath(assetsDir, hexSum)
		if err := storage.AtomicWriteFile(path, data); err != nil {
			return fmt.Errorf("write sample %s: %w", d.name, err)
		}
		asset, err := q.CreateAsset(ctx, gen.CreateAssetParams{
			ID: idgen.NewID(), ProjectID: projID, Filename: d.name + ".png",
			ContentType: "image/png", Width: int32(d.w), Height: int32(d.h),
			SizeBytes: int64(len(data)), Sha256: hexSum, StoragePath: path,
			UploadedBy: alice,
		})
		if err != nil {
			return fmt.Errorf("insert asset %s: %w", d.name, err)
		}
		layerAssets[d.layerID] = asset
	}

	compID := deterministicUUID("composition:demo")
	if _, err := q.GetComposition(ctx, compID); err != nil {
		spec := &compositor.Spec{
			CanvasWidth: 320, CanvasHeight: 240, FrameCount: 12,
			Layers: []compositor.Layer{
				{ID: "background", AssetID: layerAssets["background"].ID.String(), X: 0, Y: 0},
				{ID: "overlay", AssetID: layerAssets["overlay"].ID.String(), X: 60, Y: 50,
					Deps: []string{"background"}},
				{ID: "badge", AssetID: layerAssets["badge"].ID.String(), X: 220, Y: 16,
					Deps: []string{"overlay"}},
			},
		}
		if err := createDemoComposition(ctx, q, compID, projID, alice, spec); err != nil {
			return err
		}
	}
	return nil
}

// createDemoComposition freezes spec as version 1 of the demo composition.
func createDemoComposition(ctx context.Context, q *gen.Queries, compID, projID uuid.UUID,
	createdBy uuid.UUID, spec *compositor.Spec) error {
	if _, err := q.CreateComposition(ctx, gen.CreateCompositionParams{
		ID: compID, ProjectID: projID, Name: "Demo composition",
		CanvasWidth: int32(spec.CanvasWidth), CanvasHeight: int32(spec.CanvasHeight),
		FrameCount: int32(spec.FrameCount), CreatedBy: createdBy,
	}); err != nil {
		return err
	}
	vID := deterministicUUID("composition:demo:v1")
	canon, err := compositor.CanonicalJSON(spec)
	if err != nil {
		return err
	}
	sum := sha256.Sum256(canon)
	ver, err := q.CreateCompositionVersion(ctx, gen.CreateCompositionVersionParams{
		ID: vID, CompositionID: compID, VersionNumber: 1,
		SpecJson: canon, SpecSha256: hex.EncodeToString(sum[:]), CreatedBy: createdBy,
	})
	if err != nil {
		return err
	}
	for _, l := range spec.Layers {
		assetID, err := uuid.Parse(l.AssetID)
		if err != nil {
			return err
		}
		a, err := q.GetAssetByID(ctx, assetID)
		if err != nil {
			return err
		}
		if err := q.AddVersionResource(ctx, gen.AddVersionResourceParams{
			VersionID: ver.ID, LayerID: l.ID, AssetID: a.ID, AssetSha256: a.Sha256,
		}); err != nil {
			return err
		}
	}
	return q.SetCompositionCurrentVersion(ctx, gen.SetCompositionCurrentVersionParams{
		ID: compID, CurrentVersionID: &vID,
	})
}

// deterministicUUID returns a fixed UUID for a fixed name (v5 under a seed
// namespace), so repeated seeding finds the same project/composition.
func deterministicUUID(name string) uuid.UUID {
	ns := uuid.NewSHA1(uuid.NameSpaceURL, []byte("vfxqueue.local/seed"))
	return uuid.NewSHA1(ns, []byte(name))
}

// picture drawers ----------------------------------------------------------

func drawBackground(img *image.NRGBA) {
	b := img.Bounds()
	for y := b.Min.Y; y < b.Max.Y; y++ {
		for x := b.Min.X; x < b.Max.X; x++ {
			img.SetNRGBA(x, y, color.NRGBA{
				R: uint8(20 + x%60), G: uint8(40 + y%60), B: 120, A: 255,
			})
		}
	}
}

func drawCircle(img *image.NRGBA) {
	cx, cy := 60, 60
	b := img.Bounds()
	for y := b.Min.Y; y < b.Max.Y; y++ {
		for x := b.Min.X; x < b.Max.X; x++ {
			dx, dy := x-cx, y-cy
			d2 := dx*dx + dy*dy
			switch {
			case d2 <= 45*45:
				img.SetNRGBA(x, y, color.NRGBA{R: 230, G: 120, B: 40, A: 255})
			case d2 <= 55*55:
				img.SetNRGBA(x, y, color.NRGBA{R: 230, G: 120, B: 40, A: 90})
			}
		}
	}
}

func drawStar(img *image.NRGBA) {
	b := img.Bounds()
	for y := b.Min.Y; y < b.Max.Y; y++ {
		for x := b.Min.X; x < b.Max.X; x++ {
			if (x/10+y/10)%2 == 0 {
				img.SetNRGBA(x, y, color.NRGBA{R: 255, G: 230, B: 80, A: 230})
			}
		}
	}
}

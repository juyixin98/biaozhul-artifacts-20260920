package tests_test

import (
	"bytes"
	"encoding/json"
	"image/png"
	"testing"

	"github.com/google/uuid"

	"github.com/vfxqueue/renderq/internal/compose"
	"github.com/vfxqueue/renderq/internal/db/dbgen"
	"github.com/vfxqueue/renderq/internal/domain"
	"github.com/vfxqueue/renderq/internal/testutil"
)

// renderFrameDirect renders one frame the same way the worker does, using
// the frozen version resources and the content store.
func renderFrameDirect(t *testing.T, h *testutil.Harness, versionID uuid.UUID, rawManifest []byte, frameNo int) []byte {
	t.Helper()
	var m domain.Manifest
	if err := json.Unmarshal(rawManifest, &m); err != nil {
		t.Fatal(err)
	}
	q := dbgen.New(h.Pool)
	resources, err := q.ListVersionResources(h.Ctx, versionID)
	if err != nil {
		t.Fatal(err)
	}
	blobs := map[string][]byte{}
	resolved := map[string]domain.ResolvedAsset{}
	for _, vr := range resources {
		f, err := h.Store.OpenBlob(vr.Sha256)
		if err != nil {
			t.Fatal(err)
		}
		defer f.Close()
		var buf bytes.Buffer
		if _, err := buf.ReadFrom(f); err != nil {
			t.Fatal(err)
		}
		cfg, err := png.DecodeConfig(bytes.NewReader(buf.Bytes()))
		if err != nil {
			t.Fatal(err)
		}
		blobs[vr.Sha256] = buf.Bytes()
		resolved[vr.LayerID] = domain.ResolvedAsset{
			LayerID: vr.LayerID, AssetID: vr.AssetID.String(), SHA256: vr.Sha256,
			Width: cfg.Width, Height: cfg.Height,
		}
	}
	layers := compose.FrameLayers(&m, frameNo, blobs, resolved)
	out, err := compose.RenderFrame(m.Width, m.Height, layers)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

var _ = testutil.AdminKey

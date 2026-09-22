package apiserver

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"time"

	"vfxqueue/internal/auth"
	"vfxqueue/internal/compositor"
	"vfxqueue/internal/db/gen"
	"vfxqueue/internal/idgen"

	"github.com/google/uuid"
)

type layerReq struct {
	ID         string   `json:"id"`
	AssetID    string   `json:"asset_id"`
	X          int      `json:"x"`
	Y          int      `json:"y"`
	FrameStart int      `json:"frame_start,omitempty"`
	FrameEnd   int      `json:"frame_end,omitempty"`
	Deps       []string `json:"deps,omitempty"`
}

type createCompositionReq struct {
	Name         string     `json:"name"`
	CanvasWidth  int        `json:"canvas_width"`
	CanvasHeight int        `json:"canvas_height"`
	FrameCount   int        `json:"frame_count"`
	Layers       []layerReq `json:"layers"`
}

func (s *Server) createComposition(w http.ResponseWriter, r *http.Request) {
	proj := projectFromCtx(r.Context())
	u := auth.User(r.Context())

	var req createCompositionReq
	if err := decodeJSON(r, &req); err != nil {
		writeErr(w, 400, "invalid body: "+err.Error())
		return
	}
	if req.Name == "" {
		writeErr(w, 400, "name required")
		return
	}
	spec, err := s.buildSpec(r, proj.ID, req)
	if err != nil {
		writeErr(w, 400, err.Error())
		return
	}
	if err := spec.Validate(func(id string) bool {
		_, err := uuid.Parse(id)
		if err != nil {
			return false
		}
		a, err := s.q.GetAssetByID(r.Context(), uuid.MustParse(id))
		return err == nil && a.ProjectID == proj.ID
	}); err != nil {
		writeErr(w, 400, err.Error())
		return
	}
	canon, err := compositor.CanonicalJSON(spec)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	sum := sha256.Sum256(canon)

	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer tx.Rollback(r.Context())
	qtx := s.q.WithTx(tx)

	comp, err := qtx.CreateComposition(r.Context(), gen.CreateCompositionParams{
		ID: idgen.NewID(), ProjectID: proj.ID, Name: req.Name,
		CanvasWidth: int32(req.CanvasWidth), CanvasHeight: int32(req.CanvasHeight),
		FrameCount: int32(req.FrameCount), CreatedBy: u.ID,
	})
	if err != nil {
		writeErr(w, 400, err.Error())
		return
	}
	vNumRow, err := qtx.NextVersionNumber(r.Context(), comp.ID)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	ver, err := qtx.CreateCompositionVersion(r.Context(), gen.CreateCompositionVersionParams{
		ID: idgen.NewID(), CompositionID: comp.ID, VersionNumber: int32(vNumRow),
		SpecJson: canon, SpecSha256: hex.EncodeToString(sum[:]), CreatedBy: u.ID,
	})
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	// Freeze resources: store each layer's asset id AND the asset digest at this
	// point in time. Rendering binds to this snapshot; later asset changes
	// cannot affect it.
	for _, l := range spec.Layers {
		assetID := uuid.MustParse(l.AssetID)
		a, err := qtx.GetAssetByID(r.Context(), assetID)
		if err != nil {
			writeErr(w, 400, "layer "+l.ID+": asset not found")
			return
		}
		if err := qtx.AddVersionResource(r.Context(), gen.AddVersionResourceParams{
			VersionID: ver.ID, LayerID: l.ID, AssetID: a.ID, AssetSha256: a.Sha256,
		}); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
	}
	if err := qtx.SetCompositionCurrentVersion(r.Context(), gen.SetCompositionCurrentVersionParams{
		ID: comp.ID, CurrentVersionID: &ver.ID,
	}); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 201, map[string]any{
		"composition_id": comp.ID,
		"version_id":     ver.ID,
		"version_number": ver.VersionNumber,
		"spec_sha256":    ver.SpecSha256,
		"created_at":     time.Now(),
	})
}

func (s *Server) buildSpec(r *http.Request, projID uuid.UUID, req createCompositionReq) (*compositor.Spec, error) {
	layers := make([]compositor.Layer, 0, len(req.Layers))
	for _, l := range req.Layers {
		aid, err := uuid.Parse(l.AssetID)
		if err != nil {
			return nil, errBad("layer " + l.ID + ": asset_id must be a uuid")
		}
		a, err := s.q.GetAssetByID(r.Context(), aid)
		if err != nil {
			return nil, errBad("layer " + l.ID + ": asset not found")
		}
		if a.ProjectID != projID {
			return nil, errBad("layer " + l.ID + ": asset belongs to another project")
		}
		layers = append(layers, compositor.Layer{
			ID: l.ID, AssetID: aid.String(), X: l.X, Y: l.Y,
			FrameStart: l.FrameStart, FrameEnd: l.FrameEnd, Deps: l.Deps,
		})
	}
	return &compositor.Spec{
		CanvasWidth: req.CanvasWidth, CanvasHeight: req.CanvasHeight,
		FrameCount: req.FrameCount, Layers: layers,
	}, nil
}

func (s *Server) listCompositions(w http.ResponseWriter, r *http.Request) {
	proj := projectFromCtx(r.Context())
	rows, err := s.q.ListCompositions(r.Context(), proj.ID)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, rows)
}

type badRequest string

func errBad(m string) error        { return badRequest(m) }
func (e badRequest) Error() string { return string(e) }

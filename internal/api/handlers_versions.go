package api

import (
	"encoding/json"
	"net/http"

	"github.com/jackc/pgx/v5"

	"github.com/vfxqueue/renderq/internal/db/dbgen"
	"github.com/vfxqueue/renderq/internal/domain"
)

type createCompositionRequest struct {
	Name   string `json:"name"`
	Width  int    `json:"width"`
	Height int    `json:"height"`
}

func (s *Server) createComposition(w http.ResponseWriter, r *http.Request) {
	pid, ok := parseUUID(w, r, "projectID")
	if !ok {
		return
	}
	if !s.authorizeProject(w, r, pid) {
		return
	}
	u := currentUser(r)
	var req createCompositionRequest
	if err := decodeJSON(r, &req); err != nil || req.Name == "" || req.Width <= 0 || req.Height <= 0 {
		writeError(w, http.StatusBadRequest, `invalid body: {"name","width","height"} required`)
		return
	}
	c, err := s.q.CreateComposition(r.Context(), dbgen.CreateCompositionParams{
		ProjectID: pid, Name: req.Name,
		Width: int32(req.Width), Height: int32(req.Height), CreatedBy: u.ID,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, compositionDTOFrom(c))
}

func (s *Server) listCompositions(w http.ResponseWriter, r *http.Request) {
	pid, ok := parseUUID(w, r, "projectID")
	if !ok {
		return
	}
	if !s.authorizeProject(w, r, pid) {
		return
	}
	cs, err := s.q.ListCompositions(r.Context(), pid)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	out := make([]compositionDTO, 0, len(cs))
	for _, c := range cs {
		out = append(out, compositionDTOFrom(c))
	}
	writeJSON(w, http.StatusOK, out)
}

// getComposition authorizes via the composition's project (members only).
func (s *Server) getComposition(w http.ResponseWriter, r *http.Request) {
	cid, ok := parseUUID(w, r, "compID")
	if !ok {
		return
	}
	c, err := s.q.GetComposition(r.Context(), cid)
	if err != nil {
		writeError(w, http.StatusNotFound, "composition not found")
		return
	}
	if !s.authorizeProject(w, r, c.ProjectID) {
		return
	}
	writeJSON(w, http.StatusOK, compositionDTOFrom(c))
}

// freezeVersionRequest carries the new manifest. The manifest is validated
// (missing resources, cycles, out-of-bounds layers, escaping paths) and then
// an immutable version row plus a digest-pinned resource snapshot is written
// in one transaction.
type freezeVersionRequest struct {
	Manifest domain.Manifest `json:"manifest"`
}

func (s *Server) freezeVersion(w http.ResponseWriter, r *http.Request) {
	cid, ok := parseUUID(w, r, "compID")
	if !ok {
		return
	}
	u := currentUser(r)

	var req freezeVersionRequest
	if err := decodeJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid manifest JSON: "+err.Error())
		return
	}

	tx, err := s.pool.Begin(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer tx.Rollback(r.Context())
	qtx := s.q.WithTx(tx)

	// Lock the composition so two concurrent freezes cannot take the same
	// version_no.
	comp, err := qtx.GetCompositionForUpdate(r.Context(), cid)
	if err == pgx.ErrNoRows {
		writeError(w, http.StatusNotFound, "composition not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if !s.authorizeProject(w, r, comp.ProjectID) {
		return
	}

	if req.Manifest.Width != int(comp.Width) || req.Manifest.Height != int(comp.Height) {
		writeError(w, http.StatusBadRequest, "manifest canvas must match composition canvas")
		return
	}

	// Resolve every layer path to the current asset digest. Missing files
	// fail here; the resolved snapshot is what workers later render from.
	fetcher := func(relPath string) (string, string, int, int, error) {
		a, err := qtx.GetAssetByPath(r.Context(), dbgen.GetAssetByPathParams{
			ProjectID: comp.ProjectID, RelPath: relPath,
		})
		if err == pgx.ErrNoRows {
			return "", "", 0, 0, &missingAssetError{path: relPath}
		}
		if err != nil {
			return "", "", 0, 0, err
		}
		return a.ID.String(), a.Sha256, int(a.Width), int(a.Height), nil
	}
	resolved, err := req.Manifest.Resolve(fetcher)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid composition: "+err.Error())
		return
	}

	// Canonicalize the manifest by re-marshalling the parsed structure so
	// its digest does not depend on client whitespace/key order.
	canonical, err := json.Marshal(req.Manifest)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	nextNo, err := qtx.NextVersionNo(r.Context(), cid)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	ver, err := qtx.CreateVersion(r.Context(), dbgen.CreateVersionParams{
		CompositionID: cid, VersionNo: int64(nextNo),
		Manifest: canonical, ManifestSha256: manifestDigest(canonical), CreatedBy: u.ID,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	for _, ra := range resolved {
		assetID := uuidMustParse(ra.AssetID)
		if err := qtx.CreateVersionResource(r.Context(), dbgen.CreateVersionResourceParams{
			VersionID: ver.ID, LayerID: ra.LayerID,
			AssetID: assetID, Sha256: ra.SHA256,
		}); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
	}
	if err := tx.Commit(r.Context()); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	dto := versionDTO{
		ID: ver.ID, CompositionID: ver.CompositionID, VersionNo: ver.VersionNo,
		Manifest:       mustDecodeMap(canonical),
		ManifestSHA256: ver.ManifestSha256,
		CreatedAt:      toTime(ver.CreatedAt),
	}
	for _, ra := range resolved {
		dto.Resources = append(dto.Resources, versionResourceDTO{
			LayerID: ra.LayerID, AssetID: uuidMustParse(ra.AssetID), SHA256: ra.SHA256,
		})
	}
	writeJSON(w, http.StatusCreated, dto)
}

type missingAssetError struct{ path string }

func (e *missingAssetError) Error() string { return "missing asset " + e.path }

func (s *Server) listVersions(w http.ResponseWriter, r *http.Request) {
	cid, ok := parseUUID(w, r, "compID")
	if !ok {
		return
	}
	c, err := s.q.GetComposition(r.Context(), cid)
	if err != nil {
		writeError(w, http.StatusNotFound, "composition not found")
		return
	}
	if !s.authorizeProject(w, r, c.ProjectID) {
		return
	}
	vs, err := s.q.ListVersions(r.Context(), cid)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	out := make([]map[string]any, 0, len(vs))
	for _, v := range vs {
		out = append(out, map[string]any{
			"id": v.ID, "versionNo": v.VersionNo,
			"manifestSha256": v.ManifestSha256, "createdAt": toTime(v.CreatedAt),
		})
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) getVersion(w http.ResponseWriter, r *http.Request) {
	vid, ok := parseUUID(w, r, "versionID")
	if !ok {
		return
	}
	v, err := s.q.GetVersion(r.Context(), vid)
	if err != nil {
		writeError(w, http.StatusNotFound, "version not found")
		return
	}
	c, err := s.q.GetComposition(r.Context(), v.CompositionID)
	if err != nil {
		writeError(w, http.StatusNotFound, "composition not found")
		return
	}
	if !s.authorizeProject(w, r, c.ProjectID) {
		return
	}
	resources, err := s.q.ListVersionResources(r.Context(), v.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	dto := versionDTO{
		ID: v.ID, CompositionID: v.CompositionID, VersionNo: v.VersionNo,
		Manifest:       mustDecodeMap(v.Manifest),
		ManifestSHA256: v.ManifestSha256,
		CreatedAt:      toTime(v.CreatedAt),
	}
	for _, vr := range resources {
		dto.Resources = append(dto.Resources, versionResourceDTO{
			LayerID: vr.LayerID, AssetID: vr.AssetID, SHA256: vr.Sha256,
		})
	}
	writeJSON(w, http.StatusOK, dto)
}

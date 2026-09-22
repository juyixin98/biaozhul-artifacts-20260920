package api

import (
	"bytes"
	"errors"
	"fmt"
	"image/png"
	"io"
	"net/http"

	"github.com/vfxqueue/renderq/internal/db/dbgen"
	"github.com/vfxqueue/renderq/internal/domain"
)

const maxAssetUpload = 32 << 20 // 32 MiB

// uploadAsset accepts a PNG as multipart/form-data field "file" plus a
// "path" form field naming its project-relative location. The file is
// content-addressed: identical bytes uploaded again to the same path are
// idempotent; re-uploading a path supersedes it but frozen versions keep
// pointing at the old digest.
func (s *Server) uploadAsset(w http.ResponseWriter, r *http.Request) {
	pid, ok := parseUUID(w, r, "projectID")
	if !ok {
		return
	}
	if !s.authorizeProject(w, r, pid) {
		return
	}
	u := currentUser(r)

	if err := r.ParseMultipartForm(maxAssetUpload); err != nil {
		writeError(w, http.StatusBadRequest, "invalid multipart form (max 32MiB): "+err.Error())
		return
	}
	relPath := r.FormValue("path")
	if err := domain.ValidateRelPath(relPath); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	file, _, err := r.FormFile("file")
	if err != nil {
		writeError(w, http.StatusBadRequest, `form file field "file" is required`)
		return
	}
	defer file.Close()

	// Buffer (bounded by the 32MiB form limit) so we can decode metadata,
	// then stream into the content store.
	data, err := io.ReadAll(io.LimitReader(file, maxAssetUpload+1))
	if err != nil {
		writeError(w, http.StatusBadRequest, "read upload: "+err.Error())
		return
	}
	if int64(len(data)) > maxAssetUpload {
		writeError(w, http.StatusRequestEntityTooLarge, "asset exceeds 32MiB")
		return
	}
	cfg, err := png.DecodeConfig(bytes.NewReader(data))
	if err != nil {
		writeError(w, http.StatusBadRequest, "not a valid PNG file: "+err.Error())
		return
	}
	// Full decode rejects truncated/corrupt payloads that pass the header.
	if _, err := png.Decode(bytes.NewReader(data)); err != nil {
		writeError(w, http.StatusBadRequest, "corrupt PNG: "+err.Error())
		return
	}

	digest, size, err := s.store.PutBlob(bytes.NewReader(data))
	if err != nil {
		writeError(w, http.StatusInternalServerError, "store blob: "+err.Error())
		return
	}
	asset, err := s.q.UpsertAsset(r.Context(), dbgen.UpsertAssetParams{
		ProjectID: pid, RelPath: relPath,
		Width: int32(cfg.Width), Height: int32(cfg.Height),
		SizeBytes: size, Sha256: digest, UploadedBy: u.ID,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, assetDTOFrom(asset))
}

func (s *Server) listAssets(w http.ResponseWriter, r *http.Request) {
	pid, ok := parseUUID(w, r, "projectID")
	if !ok {
		return
	}
	if !s.authorizeProject(w, r, pid) {
		return
	}
	assets, err := s.q.ListAssets(r.Context(), pid)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	out := make([]assetDTO, 0, len(assets))
	for _, a := range assets {
		out = append(out, assetDTOFrom(a))
	}
	writeJSON(w, http.StatusOK, out)
}

var _ = fmt.Sprintf
var _ = errors.New

package apiserver

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"image/png"
	"io"
	"net/http"
	"strings"

	"vfxqueue/internal/auth"
	"vfxqueue/internal/db/gen"
	"vfxqueue/internal/idgen"
	"vfxqueue/internal/storage"
)

const maxUpload = 32 << 20 // 32 MiB

func (s *Server) uploadAsset(w http.ResponseWriter, r *http.Request) {
	proj := projectFromCtx(r.Context())
	u := auth.User(r.Context())

	r.Body = http.MaxBytesReader(w, r.Body, maxUpload)
	if err := r.ParseMultipartForm(maxUpload); err != nil {
		writeErr(w, http.StatusBadRequest, "multipart form too large or malformed")
		return
	}
	file, header, err := r.FormFile("file")
	if err != nil {
		writeErr(w, http.StatusBadRequest, `form field "file" required`)
		return
	}
	defer file.Close()

	name := header.Filename
	if name == "" || strings.ContainsAny(name, "/\\") || name == "." || name == ".." {
		writeErr(w, http.StatusBadRequest, "invalid filename")
		return
	}
	if len(name) > 255 {
		writeErr(w, http.StatusBadRequest, "filename too long")
		return
	}

	// Read fully (bounded) and verify it is a real, decodable PNG — never store
	// blindly.
	data, err := io.ReadAll(file)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "could not read upload: "+err.Error())
		return
	}
	if len(data) == 0 {
		writeErr(w, http.StatusBadRequest, "empty file")
		return
	}
	cfg, err := png.DecodeConfig(bytes.NewReader(data))
	if err != nil {
		writeErr(w, http.StatusUnsupportedMediaType, "file is not a valid PNG")
		return
	}
	if _, err := png.Decode(bytes.NewReader(data)); err != nil {
		writeErr(w, http.StatusUnsupportedMediaType, "PNG could not be fully decoded")
		return
	}
	sum := sha256.Sum256(data)
	hexSum := hex.EncodeToString(sum[:])

	// Deduplicate identical blobs within a project.
	if existing, err := s.q.GetAssetByProjectHash(r.Context(), gen.GetAssetByProjectHashParams{
		ProjectID: proj.ID, Sha256: hexSum,
	}); err == nil {
		writeJSON(w, 200, assetView(existing, true))
		return
	}

	dst := storage.AssetPath(s.cfg.AssetsDir, hexSum)
	if err := storage.AtomicWriteFile(dst, data); err != nil {
		writeErr(w, 500, fmt.Sprintf("store file: %v", err))
		return
	}
	asset, err := s.q.CreateAsset(r.Context(), gen.CreateAssetParams{
		ID: idgen.NewID(), ProjectID: proj.ID, Filename: name,
		ContentType: "image/png", Width: int32(cfg.Width), Height: int32(cfg.Height),
		SizeBytes: int64(len(data)), Sha256: hexSum, StoragePath: dst, UploadedBy: u.ID,
	})
	if err != nil {
		_ = storage.RemoveIfExists(dst)
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 201, assetView(asset, false))
}

func (s *Server) listAssets(w http.ResponseWriter, r *http.Request) {
	proj := projectFromCtx(r.Context())
	rows, err := s.q.ListAssets(r.Context(), proj.ID)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	out := make([]map[string]any, 0, len(rows))
	for _, a := range rows {
		out = append(out, assetView(a, false))
	}
	writeJSON(w, 200, out)
}

func assetView(a gen.Asset, deduplicated bool) map[string]any {
	return map[string]any{
		"id": a.ID, "filename": a.Filename, "content_type": a.ContentType,
		"width": a.Width, "height": a.Height, "size_bytes": a.SizeBytes,
		"sha256": a.Sha256, "project_id": a.ProjectID,
		"created_at": a.CreatedAt, "deduplicated": deduplicated,
	}
}

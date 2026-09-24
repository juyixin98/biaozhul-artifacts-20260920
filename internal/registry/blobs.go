package registry

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"time"

	"layerregistry/internal/digestx"
)

// ---------------------------------------------------------------------------
// Read leases
// ---------------------------------------------------------------------------

func (s *Server) createLease(w http.ResponseWriter, r *http.Request) {
	repo := r.PathValue("repo")
	var req struct {
		Kind   string `json:"kind"`
		Digest string `json:"digest"`
	}
	if err := decodeJSON(r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, "BAD_REQUEST", "invalid JSON body")
		return
	}
	if req.Kind != "blob" && req.Kind != "manifest" {
		writeErr(w, http.StatusBadRequest, "BAD_REQUEST", "kind must be blob or manifest")
		return
	}
	if !digestx.Valid(req.Digest) {
		writeErr(w, http.StatusBadRequest, "BAD_DIGEST", "invalid digest")
		return
	}
	id := newID()
	exp := time.Now().Add(s.cfg.LeaseTTL)
	if err := s.st.AddLease(r.Context(), id, repo, req.Kind, req.Digest, exp); err != nil {
		mapStoreError(w, err)
		return
	}
	w.Header().Set("X-Read-Lease", id)
	w.Header().Set("X-Read-Lease-Expires", exp.UTC().Format(time.RFC3339))
	writeJSON(w, http.StatusCreated, map[string]any{
		"lease_id": id, "expires_at": exp, "kind": req.Kind, "digest": req.Digest,
	})
}

func (s *Server) deleteLease(w http.ResponseWriter, r *http.Request) {
	if err := s.st.DeleteLease(r.Context(), r.PathValue("id")); err != nil {
		mapStoreError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) renewLease(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	exp := time.Now().Add(s.cfg.LeaseTTL)
	kind, repo, digest, err := s.st.RenewLease(r.Context(), id, exp)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"lease_id": id, "expires_at": exp, "kind": kind, "repo": repo, "digest": digest,
	})
}

// ---------------------------------------------------------------------------
// Upload sessions
// ---------------------------------------------------------------------------

// handlePostUpload dispatches:
//   - POST /v2/{repo}/blobs/uploads/?digest=...  -> monolithic single-shot
//   - POST /v2/{repo}/blobs/uploads/            -> initiate resumable session
func (s *Server) handlePostUpload(w http.ResponseWriter, r *http.Request) {
	repo := r.PathValue("repo")
	if d := r.URL.Query().Get("digest"); d != "" {
		s.monolithicUpload(w, r, repo, d)
		return
	}
	id := newID()
	f, err := s.fs.CreateTemp(id)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	_ = f.Close()
	if err := s.st.CreateUpload(r.Context(), id, repo, id); err != nil {
		_ = s.fs.RemoveTemp(id)
		mapStoreError(w, err)
		return
	}
	w.Header().Set("Location", "/v2/"+repo+"/blobs/uploads/"+id)
	w.Header().Set("Docker-Upload-UUID", id)
	w.Header().Set("Range", "0-0")
	w.WriteHeader(http.StatusAccepted)
}

func (s *Server) getUploadStatus(w http.ResponseWriter, r *http.Request) {
	u, err := s.st.GetUpload(r.Context(), r.PathValue("id"))
	if err != nil {
		mapStoreError(w, err)
		return
	}
	w.Header().Set("Docker-Upload-UUID", u.ID)
	if u.Offset > 0 {
		w.Header().Set("Range", "0-"+strconv.FormatInt(u.Offset-1, 10))
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) patchUpload(w http.ResponseWriter, r *http.Request) {
	u, err := s.st.GetUpload(r.Context(), r.PathValue("id"))
	if err != nil {
		mapStoreError(w, err)
		return
	}
	expect := u.Offset
	if cr := r.Header.Get("Content-Range"); cr != "" {
		start, _, ok := parseContentRange(cr)
		if !ok || start != expect {
			writeErr(w, http.StatusRequestedRangeNotSatisfiable, "RANGE_INVALID",
				"content-range does not match session offset "+strconv.FormatInt(expect, 10))
			return
		}
		expect = start
	}
	newOff, err := s.fs.AppendChunk(u.Name, expect, r.Body)
	if err != nil {
		writeErr(w, http.StatusRequestedRangeNotSatisfiable, "RANGE_INVALID", err.Error())
		return
	}
	if err := s.st.SetUploadOffset(r.Context(), u.ID, newOff); err != nil {
		mapStoreError(w, err)
		return
	}
	w.Header().Set("Location", "/v2/"+u.Repo+"/blobs/uploads/"+u.ID)
	w.Header().Set("Docker-Upload-UUID", u.ID)
	w.Header().Set("Range", "0-"+strconv.FormatInt(newOff-1, 10))
	w.WriteHeader(http.StatusAccepted)
}

// finalizeUpload handles PUT /v2/{repo}/blobs/uploads/{id}?digest=...
// An optional body is treated as a final monolithic chunk (it replaces the
// staged session content, matching the single-shot flow).
func (s *Server) finalizeUpload(w http.ResponseWriter, r *http.Request) {
	repo := r.PathValue("repo")
	id := r.PathValue("id")
	digest := r.URL.Query().Get("digest")
	if digest == "" {
		writeErr(w, http.StatusBadRequest, "DIGEST_MISSING", "digest query parameter required")
		return
	}
	if !digestx.Valid(digest) {
		writeErr(w, http.StatusBadRequest, "BAD_DIGEST", "invalid digest")
		return
	}
	u, err := s.st.GetUpload(r.Context(), id)
	if err != nil {
		mapStoreError(w, err)
		return
	}

	stagedName := u.Name
	if r.ContentLength > 0 {
		// Final body: stage it under a throwaway name, hash for real, swap in.
		bodyName := id + "-final"
		n, computed, err := s.fs.StreamToTemp(bodyName, r.Body)
		if err != nil {
			mapStoreError(w, err)
			return
		}
		if computed != digest {
			_ = s.fs.RemoveTemp(bodyName)
			writeErr(w, http.StatusBadRequest, "DIGEST_MISMATCH",
				"uploaded content hashes to "+computed+", expected "+digest)
			return
		}
		if err := s.fs.ReplaceTemp(u.Name, bodyName); err != nil {
			mapStoreError(w, err)
			return
		}
		_ = n
		_ = stagedName
	}

	size, err := s.publishBlob(r.Context(), repo, u.Name, digest)
	if err != nil {
		if errors.Is(err, digestx.ErrDigestMismatch) {
			writeErr(w, http.StatusBadRequest, "DIGEST_MISMATCH", err.Error())
			return
		}
		if errors.Is(err, digestx.ErrDigestFormat) {
			writeErr(w, http.StatusBadRequest, "BAD_DIGEST", err.Error())
			return
		}
		mapStoreError(w, err)
		return
	}
	_ = s.st.DeleteUpload(r.Context(), id)

	w.Header().Set("Location", "/v2/"+repo+"/blobs/"+digest)
	w.Header().Set("Docker-Content-Digest", digest)
	_ = size
	w.WriteHeader(http.StatusCreated)
}

// monolithicUpload streams the whole body straight into the staging file
// while hashing, then verifies the claimed digest before publishing.
func (s *Server) monolithicUpload(w http.ResponseWriter, r *http.Request, repo, digest string) {
	if !digestx.Valid(digest) {
		writeErr(w, http.StatusBadRequest, "BAD_DIGEST", "invalid digest")
		return
	}
	id := newID()
	// Create the session row first; StreamToTemp creates the single staging
	// file (no pre-created empty temp).
	if err := s.st.CreateUpload(r.Context(), id, repo, id); err != nil {
		mapStoreError(w, err)
		return
	}
	if _, _, err := s.fs.StreamToTemp(id, r.Body); err != nil {
		_ = s.fs.RemoveTemp(id)
		_ = s.st.DeleteUpload(r.Context(), id)
		mapStoreError(w, err)
		return
	}
	if _, err := s.publishBlob(r.Context(), repo, id, digest); err != nil {
		_ = s.fs.RemoveTemp(id)
		_ = s.st.DeleteUpload(r.Context(), id)
		if errors.Is(err, digestx.ErrDigestMismatch) {
			writeErr(w, http.StatusBadRequest, "DIGEST_MISMATCH", err.Error())
			return
		}
		mapStoreError(w, err)
		return
	}
	_ = s.st.DeleteUpload(r.Context(), id)
	w.Header().Set("Location", "/v2/"+repo+"/blobs/"+digest)
	w.Header().Set("Docker-Content-Digest", digest)
	w.WriteHeader(http.StatusCreated)
}

// publishBlob verifies the staged content with a real SHA-256 pass, renames
// it into the CAS tree, then publishes the blobs row in a transaction that
// holds the global publish lock. Crash between rename and insert leaves a
// content-addressed stray file that recovery reconciles — never a bad row.
func (s *Server) publishBlob(ctx context.Context, repo, tmpName, digest string) (int64, error) {
	size, err := s.fs.PublishTemp(tmpName, digest)
	if err != nil {
		return 0, err
	}
	if err := s.st.PublishBlob(ctx, digest, size, time.Now()); err != nil {
		return 0, err
	}
	return size, nil
}

// ---------------------------------------------------------------------------
// Blob reads / deletes
// ---------------------------------------------------------------------------

func (s *Server) headBlob(w http.ResponseWriter, r *http.Request) {
	digest := r.PathValue("digest")
	size, err := s.st.GetBlobSize(r.Context(), digest)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	w.Header().Set("Docker-Content-Digest", digest)
	w.Header().Set("Content-Length", strconv.FormatInt(size, 10))
	w.Header().Set("Content-Type", "application/octet-stream")
	w.WriteHeader(http.StatusOK)
}

// getBlob implements the pull side with a read lease. The per-blob advisory
// lock is held on a dedicated connection for the whole transfer; the GC
// sweeper takes the same lock before deleting, so it cannot delete bytes that
// a pull is streaming. An X-Read-Delay header (fault builds only) sleeps
// before sending bytes, which is what the pull/delete race test relies on.
func (s *Server) getBlob(w http.ResponseWriter, r *http.Request) {
	digest := r.PathValue("digest")
	if !digestx.Valid(digest) {
		writeErr(w, http.StatusBadRequest, "BAD_DIGEST", "invalid digest")
		return
	}

	conn, err := s.st.Pool().Acquire(r.Context())
	if err != nil {
		mapStoreError(w, err)
		return
	}
	defer conn.Release()
	if err := s.st.BlobLock(r.Context(), conn, digest); err != nil {
		mapStoreError(w, err)
		return
	}
	defer func() { _ = s.st.BlobUnlock(context.Background(), conn, digest) }()

	size, err := s.st.GetBlobSize(r.Context(), digest)
	if err != nil {
		mapStoreError(w, err)
		return
	}

	// Open bytes while holding the lock, before recording the lease.
	f, fileSize, err := s.fs.OpenBlob(digest)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	defer f.Close()

	leaseID := newID()
	exp := time.Now().Add(s.cfg.LeaseTTL)
	if err := s.st.AddLease(r.Context(), leaseID, r.PathValue("repo"), "blob", digest, exp); err != nil {
		mapStoreError(w, err)
		return
	}
	defer func() { _ = s.st.DeleteLease(context.Background(), leaseID) }()

	if s.cfg.FaultsEnabled {
		if d := r.Header.Get("X-Read-Delay"); d != "" {
			if ms, perr := strconv.Atoi(d); perr == nil {
				select {
				case <-time.After(time.Duration(ms) * time.Millisecond):
				case <-r.Context().Done():
				}
			}
		}
	}

	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Length", strconv.FormatInt(fileSize, 10))
	w.Header().Set("Docker-Content-Digest", digest)
	w.Header().Set("X-Read-Lease", leaseID)
	w.Header().Set("X-Read-Lease-Expires", exp.UTC().Format(time.RFC3339))
	w.WriteHeader(http.StatusOK)
	_, _ = io.Copy(w, f)
	_ = size
}

func (s *Server) deleteBlob(w http.ResponseWriter, r *http.Request) {
	digest := r.PathValue("digest")
	// Remove bytes after the row delete; this mirrors the sweep quarantine
	// order for the API path.
	if err := s.st.DeleteBlobAPI(r.Context(), digest); err != nil {
		mapStoreError(w, err)
		return
	}
	if err := s.fs.RemoveBlob(digest); err != nil {
		// Row is gone; the stray file is reconciled by recovery. Log via audit.
		_ = s.st.AuditEvent(r.Context(), "", "api.delete_blob_leftover", "blob", "", digest, err.Error())
	}
	_ = s.st.AuditEvent(r.Context(), "", "api.delete_blob", "blob", "", digest, "")
	w.WriteHeader(http.StatusAccepted)
}

func decodeJSON(r *http.Request, v any) error {
	return json.NewDecoder(r.Body).Decode(v)
}

package registry

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"layerregistry/internal/digestx"
	"layerregistry/internal/manifestx"
	"layerregistry/internal/store"
)

// putManifest validates and publishes a manifest document.
//
//	PUT /v2/{repo}/manifests/{ref}   ref = tag name or sha256 digest
//
// The bytes are read fully, hashed for real, parsed, every referenced object
// is required to already exist, and the manifest + edges + tag are inserted
// in one transaction holding the global publish lock.
func (s *Server) putManifest(w http.ResponseWriter, r *http.Request) {
	repo := r.PathValue("repo")
	ref := r.PathValue("ref")
	mediaType := r.Header.Get("Content-Type")
	// Accept "; charset=..." suffixes.
	if i := strings.IndexByte(mediaType, ';'); i >= 0 {
		mediaType = strings.TrimSpace(mediaType[:i])
	}
	if !manifestx.IsManifestMediaType(mediaType) {
		writeErr(w, http.StatusBadRequest, "UNSUPPORTED_MEDIA_TYPE",
			"content-type must be a supported OCI/Docker manifest type")
		return
	}

	content, err := io.ReadAll(r.Body)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	digest := digestx.FromBytes(content)

	tagName := ""
	if strings.HasPrefix(ref, "sha256:") {
		if ref != digest {
			writeErr(w, http.StatusBadRequest, "DIGEST_MISMATCH",
				"digest reference "+ref+" does not match content digest "+digest)
			return
		}
	} else {
		if !validTagName(ref) {
			writeErr(w, http.StatusBadRequest, "BAD_TAG", "invalid tag reference: "+ref)
			return
		}
		tagName = ref
	}

	refs, err := manifestx.Parse(content, mediaType)
	if err != nil {
		if errors.Is(err, manifestx.ErrUnsupportedManifest) {
			writeErr(w, http.StatusBadRequest, "MANIFEST_INVALID", err.Error())
			return
		}
		mapStoreError(w, err)
		return
	}

	tx, err := s.st.Pool().Begin(r.Context())
	if err != nil {
		mapStoreError(w, err)
		return
	}
	defer tx.Rollback(r.Context())
	if err := store.TxPublishLock(r.Context(), tx); err != nil {
		mapStoreError(w, err)
		return
	}
	// Referential integrity: all children must exist before we accept.
	for _, cr := range refs {
		ok, err := childExistsTx(r.Context(), tx, repo, cr)
		if err != nil {
			mapStoreError(w, err)
			return
		}
		if !ok {
			writeErr(w, http.StatusBadRequest, "BLOB_UNKNOWN",
				"manifest references missing "+cr.Kind+" "+cr.Digest+" (publish children first)")
			return
		}
	}

	dbRefs := make([]store.ChildRef, 0, len(refs))
	for _, cr := range refs {
		dbRefs = append(dbRefs, store.ChildRef{Kind: cr.Kind, Digest: cr.Digest})
	}
	if err := s.st.UpsertManifest(r.Context(), tx, repo, digest, mediaType,
		int64(len(content)), content, dbRefs, tagName); err != nil {
		mapStoreError(w, err)
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		mapStoreError(w, err)
		return
	}

	// "标签更新产生引用和读租约": a tag update briefly pins the new manifest.
	if tagName != "" {
		lid := newID()
		_ = s.st.AddLease(r.Context(), lid, repo, "manifest", digest, time.Now().Add(s.cfg.LeaseTTL))
		// Short-lived by TTL; no body is streaming, but the row exists while
		// the change is visible to concurrent GC.
	}

	w.Header().Set("Docker-Content-Digest", digest)
	w.Header().Set("Location", "/v2/"+repo+"/manifests/"+digest)
	w.WriteHeader(http.StatusCreated)
}

func childExistsTx(ctx context.Context, tx pgx.Tx, repo string, cr manifestx.ChildRef) (bool, error) {
	switch cr.Kind {
	case "blob":
		var ok bool
		err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM blobs WHERE digest=$1)", cr.Digest).Scan(&ok)
		return ok, err
	default:
		var ok bool
		err := tx.QueryRow(ctx,
			"SELECT EXISTS(SELECT 1 FROM manifests WHERE repo=$1 AND digest=$2)", repo, cr.Digest).Scan(&ok)
		return ok, err
	}
}

// resolveManifestRef maps a tag/digest ref to an actual manifest digest.
func (s *Server) resolveManifestRef(ctx context.Context, repo, ref string) (string, error) {
	if strings.HasPrefix(ref, "sha256:") {
		ok, err := s.st.ManifestExists(ctx, repo, ref)
		if err != nil {
			return "", err
		}
		if !ok {
			return "", store.ErrNotFound
		}
		return ref, nil
	}
	return s.st.ResolveTag(ctx, repo, ref)
}

func (s *Server) headManifest(w http.ResponseWriter, r *http.Request) {
	repo := r.PathValue("repo")
	ref := r.PathValue("ref")
	digest, err := s.resolveManifestRef(r.Context(), repo, ref)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	m, err := s.st.GetManifest(r.Context(), repo, digest)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	w.Header().Set("Docker-Content-Digest", digest)
	w.Header().Set("Content-Type", m.MediaType)
	w.Header().Set("Content-Length", strconv.FormatInt(m.Size, 10))
	w.WriteHeader(http.StatusOK)
}

func (s *Server) getManifest(w http.ResponseWriter, r *http.Request) {
	repo := r.PathValue("repo")
	ref := r.PathValue("ref")
	digest, err := s.resolveManifestRef(r.Context(), repo, ref)
	if err != nil {
		mapStoreError(w, err)
		return
	}

	conn, err := s.st.Pool().Acquire(r.Context())
	if err != nil {
		mapStoreError(w, err)
		return
	}
	defer conn.Release()
	if err := s.st.ManifestLock(r.Context(), conn, repo, digest); err != nil {
		mapStoreError(w, err)
		return
	}
	defer func() { _ = s.st.ManifestUnlock(context.Background(), conn, repo, digest) }()

	m, err := s.st.GetManifest(r.Context(), repo, digest)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	leaseID := newID()
	exp := time.Now().Add(s.cfg.LeaseTTL)
	if err := s.st.AddLease(r.Context(), leaseID, repo, "manifest", digest, exp); err != nil {
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

	w.Header().Set("Content-Type", m.MediaType)
	w.Header().Set("Docker-Content-Digest", digest)
	w.Header().Set("X-Read-Lease", leaseID)
	w.Header().Set("X-Read-Lease-Expires", exp.UTC().Format(time.RFC3339))
	w.WriteHeader(http.StatusOK)
	_, _ = io.Copy(w, bytes.NewReader(m.Content))
}

func (s *Server) deleteManifest(w http.ResponseWriter, r *http.Request) {
	repo := r.PathValue("repo")
	ref := r.PathValue("ref")
	digest, err := s.st.DeleteTagOrManifest(r.Context(), repo, ref)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	_ = s.st.AuditEvent(r.Context(), "", "api.delete_manifest", "manifest", repo, digest, "ref="+ref)
	w.WriteHeader(http.StatusAccepted)
}

func (s *Server) listTags(w http.ResponseWriter, r *http.Request) {
	repo := r.PathValue("repo")
	tags, err := s.st.ListTags(r.Context(), repo)
	if err != nil {
		mapStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"name": repo, "tags": tags})
}

func validTagName(tag string) bool {
	if tag == "" || len(tag) > 128 {
		return false
	}
	if tag == "." || tag == ".." {
		return false
	}
	for _, c := range tag {
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9':
		case c == '.' || c == '-' || c == '_' || c == '/':
		default:
			return false
		}
	}
	return true
}

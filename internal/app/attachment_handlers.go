package app

import (
	"archive/zip"
	"bytes"
	"encoding/base64"
	"fmt"
	"net/http"

	"community-governance/internal/database"
	"community-governance/internal/httpx"
)

// addAttachment binds a binary blob to an immutable version. Only the author
// may upload, and only to the current (editable) version — a published version
// is frozen forever.
func (a *App) addAttachment(w http.ResponseWriter, r *http.Request) {
	vid, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	var req struct {
		Filename    string `json:"filename"`
		ContentType string `json:"content_type"`
		// Base64 encoded blob (standard encoding, padding optional).
		DataBase64 string `json:"data_base64"`
	}
	if err := httpx.Decode(r, &req); err != nil || req.Filename == "" || req.DataBase64 == "" {
		badRequest(w, "filename and data_base64 required")
		return
	}
	data, err := base64.StdEncoding.DecodeString(req.DataBase64)
	if err != nil {
		badRequest(w, "data_base64 must be valid base64")
		return
	}
	if req.ContentType == "" {
		req.ContentType = "application/octet-stream"
	}

	var att database.AddAttachmentRow
	txErr := a.withTx(r.Context(), func(q *database.Queries) error {
		v, err := q.GetVersion(r.Context(),
			database.GetVersionParams{ID: vid, CommunityID: u.CommunityID})
		if err != nil {
			return err
		}
		c, err := q.GetContentForUpdate(r.Context(),
			database.GetContentForUpdateParams{ID: v.ContentID, CommunityID: u.CommunityID})
		if err != nil {
			return err
		}
		if c.AuthorID != u.ID {
			return errForbidden
		}
		if !c.CurrentVersionID.Valid || c.CurrentVersionID.Int64 != vid {
			// Attachments are frozen with the version. Editing a published or
			// superseded version is impossible.
			return fmt.Errorf("can only attach files to the current editable version: %w", errConflict)
		}
		att, err = q.AddAttachment(r.Context(), database.AddAttachmentParams{
			CommunityID: u.CommunityID, VersionID: vid,
			Filename: req.Filename, ContentType: req.ContentType, Data: data,
		})
		return err
	})
	if txErr != nil {
		writeErr(w, txErr)
		return
	}
	httpx.JSON(w, http.StatusCreated, att)
}

func (a *App) listAttachments(w http.ResponseWriter, r *http.Request) {
	vid, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	v, err := a.Q.GetVersion(r.Context(),
		database.GetVersionParams{ID: vid, CommunityID: u.CommunityID})
	if err != nil {
		writeErr(w, err)
		return
	}
	// Same choke-point as body read and export.
	if _, err := a.resolveReadableVersion(r.Context(), a.Q, u, v.ContentID, vid); err != nil {
		forbidden(w, err.Error())
		return
	}
	atts, err := a.Q.ListAttachmentsMeta(r.Context(), vid)
	if err != nil {
		serverError(w, err)
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"attachments": atts})
}

// getAttachment streams one file after running the identical authorization
// check used for body reads and export.
func (a *App) getAttachment(w http.ResponseWriter, r *http.Request) {
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	att, err := a.Q.GetAttachment(r.Context(),
		database.GetAttachmentParams{ID: id, CommunityID: u.CommunityID})
	if err != nil {
		writeErr(w, err)
		return
	}
	v, err := a.Q.GetVersion(r.Context(),
		database.GetVersionParams{ID: att.VersionID, CommunityID: u.CommunityID})
	if err != nil {
		writeErr(w, err)
		return
	}
	if _, err := a.resolveReadableVersion(r.Context(), a.Q, u, v.ContentID, att.VersionID); err != nil {
		forbidden(w, err.Error())
		return
	}
	w.Header().Set("Content-Type", att.ContentType)
	w.Header().Set("Content-Disposition",
		fmt.Sprintf(`attachment; filename="%s"`, att.Filename))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(att.Data)
}

// exportContent builds a zip of the authorized version's body plus all of its
// attachments. Authorization is the single resolveReadableVersion call, so
// export can never leak a version the caller could not read directly.
func (a *App) exportContent(w http.ResponseWriter, r *http.Request) {
	id, err := urlID(r, "id")
	if err != nil {
		writeErr(w, err)
		return
	}
	u := currentUser(r)
	res, err := a.resolveReadableVersion(r.Context(), a.Q, u, id, 0)
	if err != nil {
		forbidden(w, err.Error())
		return
	}
	blobs, err := a.Q.ListAttachmentBlobs(r.Context(), res.Version.ID)
	if err != nil {
		serverError(w, err)
		return
	}

	buf := &bytes.Buffer{}
	zw := zip.NewWriter(buf)
	fw, err := zw.Create("body.txt")
	if err != nil {
		serverError(w, err)
		return
	}
	header := fmt.Sprintf("Content #%d - %s\nVersion %d (id=%d)\n\n",
		res.Content.ID, res.Content.Title, res.Version.VersionNo, res.Version.ID)
	if _, err := fw.Write([]byte(header + res.Version.Body)); err != nil {
		serverError(w, err)
		return
	}
	for _, b := range blobs {
		f, err := zw.Create("attachments/" + safeZipName(b.Filename, b.ID))
		if err != nil {
			serverError(w, err)
			return
		}
		if _, err := f.Write(b.Data); err != nil {
			serverError(w, err)
			return
		}
	}
	if err := zw.Close(); err != nil {
		serverError(w, err)
		return
	}

	w.Header().Set("Content-Type", "application/zip")
	w.Header().Set("Content-Disposition",
		fmt.Sprintf(`attachment; filename="content-%d-v%d.zip"`, res.Content.ID, res.Version.VersionNo))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(buf.Bytes())
}

func safeZipName(name string, id int64) string {
	// Prefix with id to guarantee uniqueness within the zip.
	return fmt.Sprintf("%d-%s", id, name)
}

package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"gorm.io/gorm"

	"proofcycle/internal/model"
	"proofcycle/internal/storage"
)

// UploadRevision streams and validates a new file revision, then activates it.
//
// The slow work (streaming to a random temp file with the size cap, magic-byte
// sniff and SHA-256) happens before any database write. Activation then runs
// inside ONE transaction that holds SELECT ... FOR UPDATE on the job row, so
// concurrent uploads, opinion edits and sign-offs are fully serialized:
//
//  1. reserve version = current_version + 1 and snapshot the checklist;
//  2. atomically rename the temp file to its server-generated final path;
//  3. supersede the previous version/round and advance current_version.
//
// Crash windows and their handling at startup (RecoverOrphans):
//   - during streaming / before commit: temp file purged, no database rows;
//   - between rename and commit: an unreferenced file is garbage-collected;
//   - after commit: version, round, snapshot and file are all visible together.
func (s *Service) UploadRevision(ctx context.Context, caller *model.User, jobID int64, r io.Reader) (*model.FileVersion, *model.ReviewRound, error) {
	if caller.Role != model.RoleDesigner {
		return nil, nil, errForbidden("only the designer can upload revisions")
	}

	// Phase 1: validate into temp. No database state exists yet, so a client
	// disconnect simply removes the temp file.
	saved, err := s.store.SaveUploadStream(ctx, r, s.maxSize)
	if err != nil {
		return nil, nil, mapUploadError(err)
	}

	var fv model.FileVersion
	var round model.ReviewRound
	txErr := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		job, lerr := lockJob(ctx, tx, jobID)
		if lerr != nil {
			return lerr
		}
		if job.DesignerID != caller.ID {
			return errForbidden("you are not the designer of this job")
		}
		if job.Status == model.JobStatusApproved {
			return errConflict("job is already approved and accepts no further revisions")
		}
		template, lerr := loadChecklistTemplate(ctx, tx, jobID)
		if lerr != nil {
			return lerr
		}
		newVersion := job.CurrentVersion + 1
		finalPath, lerr := s.store.FinalPath(jobID, newVersion, saved.Ext)
		if lerr != nil {
			return errBadRequest("cannot generate storage path: " + lerr.Error())
		}

		// Phase 2: move the validated temp file into place (same-filesystem
		// atomic rename). On failure the transaction rolls back and the temp
		// file is removed by the caller after the transaction returns.
		if cErr := s.store.Commit(saved.TempPath, finalPath); cErr != nil {
			return fmt.Errorf("commit file to storage failed: %w", cErr)
		}

		// Supersede the previous current version and its open round BEFORE
		// inserting the new rows: otherwise, within this same transaction,
		// these UPDATEs would also match the just-inserted v1/open row. Prior
		// opinions are retained as history but belong to a superseded round, so
		// they can never justify approval of the new version.
		if cErr := tx.Model(&model.FileVersion{}).
			Where("job_id = ? AND status = ?", jobID, model.VersionStatusReady).
			Update("status", model.VersionStatusSuperseded).Error; cErr != nil {
			return cErr
		}
		if cErr := tx.Model(&model.ReviewRound{}).
			Where("job_id = ? AND status = ?", jobID, model.RoundStatusOpen).
			Update("status", model.RoundStatusSuperseded).Error; cErr != nil {
			return cErr
		}

		now := time.Now().UTC()
		fv = model.FileVersion{
			JobID:      jobID,
			Version:    newVersion,
			FileName:   "", // display name is attached later, never used on disk
			StoredPath: finalPath,
			SHA256:     saved.SHA256,
			SizeBytes:  saved.Size,
			MimeType:   saved.MimeType,
			UploadedBy: caller.ID,
			Status:     model.VersionStatusReady,
			CreatedAt:  now,
		}
		if cErr := tx.Create(&fv).Error; cErr != nil {
			return cErr // unique (job, version) cannot occur under the job lock
		}
		round = model.ReviewRound{
			JobID: jobID, Version: newVersion, FileVerID: fv.ID,
			Status: model.RoundStatusOpen, CreatedAt: now,
		}
		if cErr := tx.Create(&round).Error; cErr != nil {
			return cErr
		}
		for _, c := range template {
			if cErr := tx.Create(&model.ChecklistItem{
				RoundID: round.ID, ItemOrder: c.ItemOrder, Code: c.Code, Description: c.Description,
			}).Error; cErr != nil {
				return cErr
			}
		}

		if cErr := tx.Model(&model.Job{}).Where("id = ?", jobID).
			Updates(map[string]any{"current_version": newVersion, "updated_at": now}).Error; cErr != nil {
			return cErr
		}
		return nil
	})
	if txErr != nil {
		// Rollback happened; undo any filesystem side effect.
		_ = s.store.Remove(saved.TempPath)
		if fv.StoredPath != "" {
			_ = s.store.Remove(fv.StoredPath)
		}
		return nil, nil, txErr
	}
	return &fv, &round, nil
}

// SetOriginalName records the display name claimed by the upload request. It is
// metadata only: the on-disk path is always server-generated.
func (s *Service) SetOriginalName(ctx context.Context, caller *model.User, versionID int64, name string) error {
	safe := safeName(name)
	if safe == "" {
		return nil
	}
	return s.db.WithContext(ctx).Model(&model.FileVersion{}).
		Where("id = ? AND uploaded_by = ?", versionID, caller.ID).
		Update("file_name", safe).Error
}

// RecoverOrphans restores on-disk consistency after a crash:
//   - purge leftover staging uploads;
//   - delete committed files that no file_versions row references (a rename
//     that landed just before its transaction was lost);
//   - verify every referenced file still hashes to its recorded SHA-256; a
//     mismatch is reported rather than silently served.
func (s *Service) RecoverOrphans(ctx context.Context) error {
	if err := s.store.PurgeTemp(); err != nil {
		return err
	}

	var all []model.FileVersion
	if err := s.db.WithContext(ctx).Find(&all).Error; err != nil {
		return err
	}
	known := map[string]struct{}{}
	for _, v := range all {
		known[v.StoredPath] = struct{}{}
	}
	onDisk, err := s.store.WalkFiles()
	if err != nil {
		return err
	}
	for path := range onDisk {
		if _, ok := known[path]; !ok {
			_ = s.store.Remove(path)
		}
	}

	var corrupted []string
	for _, v := range all {
		if verifyErr := verifyStoredFile(v.StoredPath, v.SHA256, v.SizeBytes); verifyErr != nil {
			corrupted = append(corrupted, fmt.Sprintf("job %d v%d: %v", v.JobID, v.Version, verifyErr))
		}
	}
	if len(corrupted) > 0 {
		return fmt.Errorf("integrity check failed for stored files: %s", strings.Join(corrupted, "; "))
	}
	return nil
}

// DownloadVersion opens a specific version's immutable file. The content is
// re-hashed against the recorded SHA-256 so a tampered or partially written
// file is never served.
func (s *Service) DownloadVersion(ctx context.Context, caller *model.User, jobID int64, version int) (*os.File, *model.FileVersion, error) {
	if _, _, err := s.authorizeParticipant(ctx, caller, jobID); err != nil {
		return nil, nil, err
	}
	var fv model.FileVersion
	err := s.db.WithContext(ctx).
		Where("job_id = ? AND version = ?", jobID, version).
		First(&fv).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, nil, errNotFound("file version not found")
	}
	if err != nil {
		return nil, nil, err
	}
	if err = verifyStoredFile(fv.StoredPath, fv.SHA256, fv.SizeBytes); err != nil {
		return nil, nil, &Error{Status: httpStatusInternal, Code: "file_integrity", Message: err.Error()}
	}
	f, err := s.store.Open(fv.StoredPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil, errNotFound("stored file is missing")
		}
		return nil, nil, err
	}
	return f, &fv, nil
}

func loadChecklistTemplate(ctx context.Context, tx *gorm.DB, jobID int64) ([]model.JobChecklistItem, error) {
	var items []model.JobChecklistItem
	if err := tx.WithContext(ctx).Where("job_id = ?", jobID).
		Order("item_order ASC").Find(&items).Error; err != nil {
		return nil, err
	}
	if len(items) == 0 {
		return nil, errUnprocessable("checklist_missing", "job has no checklist to snapshot")
	}
	return items, nil
}

func verifyStoredFile(path, wantSHA string, wantSize int64) error {
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("stored file unavailable: %w", err)
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return fmt.Errorf("stored file unreadable: %w", err)
	}
	if n != wantSize || hex.EncodeToString(h.Sum(nil)) != wantSHA {
		return fmt.Errorf("stored file failed integrity verification")
	}
	return nil
}

func mapUploadError(err error) error {
	var se *Error
	if errors.As(err, &se) {
		return se
	}
	switch {
	case errors.Is(err, storage.ErrTooLarge):
		return errUnprocessable("file_too_large", err.Error())
	case errors.Is(err, storage.ErrUnsupportedFile):
		return errUnprocessable("unsupported_file_type", err.Error())
	default:
		return errBadRequest("upload failed: " + err.Error())
	}
}

func safeName(name string) string {
	clean := name
	for len(clean) > 0 && (clean[0] == '/' || clean[0] == '.' || clean[0] == '\\') {
		clean = clean[1:]
	}
	if i := indexAny(clean, "/\\"); i >= 0 {
		clean = clean[:i]
	}
	if len(clean) > 255 {
		clean = clean[:255]
	}
	return clean
}

func indexAny(s, chars string) int {
	for i := 0; i < len(s); i++ {
		for j := 0; j < len(chars); j++ {
			if s[i] == chars[j] {
				return i
			}
		}
	}
	return -1
}

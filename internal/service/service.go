package service

import (
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"
	"time"

	"proofcycle/internal/models"
	"proofcycle/internal/storage"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

// Service holds the business logic.
type Service struct {
	db    *gorm.DB
	store *storage.Store
}

func New(gdb *gorm.DB, store *storage.Store) *Service {
	return &Service{db: gdb, store: store}
}

// Store exposes the file store (used for downloads and orphan sweep).
func (s *Service) Store() *storage.Store { return s.store }

// ChecklistVersion is the snapshot label for the seeded master checklist.
const ChecklistVersion = "v1"

// ---------------------------------------------------------------------------
// Job lifecycle
// ---------------------------------------------------------------------------

// CreateJobInput carries the job creation request.
type CreateJobInput struct {
	Title       string
	Description string
	DesignerID  uint
	PMID        uint
	ReviewerIDs []uint
}

// CreateJob validates the roster (designer, PM, 1..8 distinct reviewers) and
// persists the job.
func (s *Service) CreateJob(in CreateJobInput) (*models.Job, error) {
	title := strings.TrimSpace(in.Title)
	if title == "" || len(title) > 200 {
		return nil, fmt.Errorf("%w: title is required (max 200 chars)", ErrInvalidInput)
	}
	if in.DesignerID == 0 || in.PMID == 0 {
		return nil, fmt.Errorf("%w: designer and PM are required", ErrInvalidInput)
	}

	ids := dedupe(in.ReviewerIDs)
	if len(ids) < 1 || len(ids) > 8 {
		return nil, ErrReviewerRoster
	}
	for _, id := range ids {
		if id == in.DesignerID || id == in.PMID {
			return nil, ErrPMConflict
		}
	}
	if in.DesignerID == in.PMID {
		return nil, ErrDesignerConflict
	}

	var users []models.User
	allIDs := append(append(append([]uint{}, ids...), in.DesignerID), in.PMID)
	if err := s.db.Where("id IN ?", dedupe(allIDs)).Find(&users).Error; err != nil {
		return nil, err
	}
	byID := make(map[uint]models.User, len(users))
	for _, u := range users {
		byID[u.ID] = u
	}
	d, ok := byID[in.DesignerID]
	if !ok || d.Role != models.RoleDesigner {
		return nil, fmt.Errorf("%w: designer id %d is not a designer", ErrInvalidInput, in.DesignerID)
	}
	pm, ok := byID[in.PMID]
	if !ok || pm.Role != models.RolePM {
		return nil, fmt.Errorf("%w: pm id %d is not a project manager", ErrInvalidInput, in.PMID)
	}
	for _, id := range ids {
		u, ok := byID[id]
		if !ok || u.Role != models.RoleReviewer {
			return nil, ErrReviewerRole
		}
	}

	job := models.Job{
		Title:       title,
		Description: strings.TrimSpace(in.Description),
		DesignerID:  in.DesignerID,
		PMID:        in.PMID,
		Status:      models.JobOpen,
	}
	err := s.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Create(&job).Error; err != nil {
			return err
		}
		for _, id := range ids {
			jr := models.JobReviewer{JobID: job.ID, UserID: id}
			if err := tx.Create(&jr).Error; err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &job, nil
}

// GetJob loads a job with its roster. found==false means 404 regardless of
// membership so unauthorized probing cannot distinguish missing jobs.
func (s *Service) GetJob(jobID uint) (*models.Job, []models.JobReviewer, bool, error) {
	var job models.Job
	if err := s.db.First(&job, jobID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, nil, false, nil
		}
		return nil, nil, false, err
	}
	var reviewers []models.JobReviewer
	if err := s.db.Preload("User").Where("job_id = ?", jobID).Find(&reviewers).Error; err != nil {
		return nil, nil, false, err
	}
	return &job, reviewers, true, nil
}

// ---------------------------------------------------------------------------
// Versions & rounds
// ---------------------------------------------------------------------------

// SaveFirstVersion uploads the initial file and opens round 1 with a checklist
// snapshot. The blob is written first; if any database step fails the blob is
// removed, so no orphan is observable.
func (s *Service) SaveFirstVersion(jobID, uploaderID uint, filename string, r io.Reader) (*models.FileVersion, *models.Round, error) {
	saved, err := s.store.Save(r, jobID)
	if err != nil {
		return nil, nil, mapStoreErr(err)
	}

	var version models.FileVersion
	var round models.Round
	txErr := s.db.Transaction(func(tx *gorm.DB) error {
		job, _, found, lerr := s.loadJobLocked(tx, jobID)
		if lerr != nil {
			return lerr
		}
		if !found {
			return ErrNotFound
		}
		if job.Status == models.JobApproved {
			return ErrJobClosed
		}
		// Only the designer or the PM may place files on a job; an assigned
		// reviewer is a member but lacks this role.
		if uploaderID != job.DesignerID && uploaderID != job.PMID {
			return ErrRoleDenied
		}

		var count int64
		if err := tx.Model(&models.FileVersion{}).Where("job_id = ?", jobID).Count(&count).Error; err != nil {
			return err
		}
		if count > 0 {
			return fmt.Errorf("%w: initial version already exists", ErrInvalidInput)
		}

		version = models.FileVersion{
			JobID:       jobID,
			Version:     1,
			Filename:    sanitizeFilename(filename),
			Kind:        saved.Kind.Name,
			Size:        saved.Size,
			SHA256:      saved.SHA256,
			StoragePath: saved.RelPath,
			UploadedBy:  uploaderID,
		}
		if err := tx.Create(&version).Error; err != nil {
			return err
		}
		return s.openRound(tx, jobID, &version, &round)
	})
	if txErr != nil {
		_ = s.store.Discard(saved.RelPath) // roll back the committed blob
		return nil, nil, txErr
	}
	return &version, &round, nil
}

// SubmitRevision uploads a new revision, supersedes the current round
// (preserving its opinions as history) and opens the next round. Only the
// job's designer or PM may do this; an approved job is closed.
func (s *Service) SubmitRevision(jobID, actorID uint, filename string, r io.Reader) (*models.FileVersion, *models.Round, error) {
	saved, err := s.store.Save(r, jobID)
	if err != nil {
		return nil, nil, mapStoreErr(err)
	}

	var version models.FileVersion
	var round models.Round
	txErr := s.db.Transaction(func(tx *gorm.DB) error {
		job, reviewers, found, err := s.loadJobLocked(tx, jobID)
		if err != nil {
			return err
		}
		if !found {
			return ErrNotFound
		}
		if job.Status == models.JobApproved {
			return ErrJobClosed
		}
		if !canContribute(actorID, job, reviewers) {
			if isMember(actorID, job, reviewers) {
				return ErrRoleDenied
			}
			return ErrForbidden
		}

		var versionCount int64
		if err := tx.Model(&models.FileVersion{}).Where("job_id = ?", jobID).Count(&versionCount).Error; err != nil {
			return err
		}
		if versionCount == 0 {
			return fmt.Errorf("%w: upload the initial version before submitting a revision", ErrInvalidInput)
		}

		var last models.FileVersion
		if err := tx.Where("job_id = ?", jobID).Order("version DESC").First(&last).Error; err != nil {
			return err
		}

		// Supersede the live round. Old opinions stay in the database and are
		// queryable via history but can never approve the new version.
		if err := tx.Model(&models.Round{}).
			Where("job_id = ? AND active_flag = 1", jobID).
			Updates(map[string]any{"status": models.RoundSuperseded, "active_flag": nil}).Error; err != nil {
			return err
		}

		version = models.FileVersion{
			JobID:       jobID,
			Version:     last.Version + 1,
			Filename:    sanitizeFilename(filename),
			Kind:        saved.Kind.Name,
			Size:        saved.Size,
			SHA256:      saved.SHA256,
			StoragePath: saved.RelPath,
			UploadedBy:  actorID,
		}
		if err := tx.Create(&version).Error; err != nil {
			return err
		}
		return s.openRound(tx, jobID, &version, &round)
	})
	if txErr != nil {
		_ = s.store.Discard(saved.RelPath)
		return nil, nil, txErr
	}
	return &version, &round, nil
}

// openRound snapshots the master checklist into a new active round.
func (s *Service) openRound(tx *gorm.DB, jobID uint, version *models.FileVersion, round *models.Round) error {
	var items []models.ChecklistTemplateItem
	if err := tx.Order("`order` ASC, id ASC").Find(&items).Error; err != nil {
		return err
	}
	if len(items) == 0 {
		return errors.New("checklist template is empty")
	}

	*round = models.Round{
		JobID:            jobID,
		VersionID:        version.ID,
		VersionNumber:    version.Version,
		ChecklistVersion: ChecklistVersion,
		Status:           models.RoundActive,
		ActiveFlag:       boolPtr(1),
	}
	if err := tx.Create(round).Error; err != nil {
		return err
	}
	for _, it := range items {
		ri := models.RoundItem{
			RoundID:     round.ID,
			Code:        it.Code,
			Description: it.Description,
			SortOrder:   it.Order,
		}
		if err := tx.Create(&ri).Error; err != nil {
			return err
		}
	}
	return nil
}

// ActiveRound returns the live round for a job.
func (s *Service) ActiveRound(jobID uint) (*models.Round, error) {
	var round models.Round
	err := s.db.Where("job_id = ? AND active_flag = 1", jobID).First(&round).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrRoundInactive
	}
	if err != nil {
		return nil, err
	}
	return &round, nil
}

// RoundByVersion locates the round bound to a specific file version.
func (s *Service) RoundByVersion(jobID uint, versionNumber int) (*models.Round, error) {
	var round models.Round
	err := s.db.Where("job_id = ? AND version_number = ?", jobID, versionNumber).First(&round).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrVersionMismatch
	}
	if err != nil {
		return nil, err
	}
	return &round, nil
}

// GetVersion loads a single file version.
func (s *Service) GetVersion(jobID uint, versionNumber int) (*models.FileVersion, error) {
	var v models.FileVersion
	err := s.db.Where("job_id = ? AND version = ?", jobID, versionNumber).First(&v).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, ErrVersionMismatch
	}
	if err != nil {
		return nil, err
	}
	return &v, nil
}

// ListVersions returns all versions of a job oldest-first.
func (s *Service) ListVersions(jobID uint) ([]models.FileVersion, error) {
	var versions []models.FileVersion
	if err := s.db.Where("job_id = ?", jobID).Order("version ASC").Find(&versions).Error; err != nil {
		return nil, err
	}
	return versions, nil
}

// ---------------------------------------------------------------------------
// Opinions
// ---------------------------------------------------------------------------

// ItemOpinion is one item verdict in an upsert batch.
type ItemOpinion struct {
	Code    string `json:"code"`
	Outcome string `json:"outcome"`
	Reason  string `json:"reason"`
}

// UpsertOpinions writes (create or update) a reviewer's opinions for the
// active round. Existing opinions carry an optimistic-lock version; a stale
// expected version rejects the whole batch. A failed item always requires a
// reason; outcomes are limited to pass/fail/na.
func (s *Service) UpsertOpinions(jobID, reviewerID uint, expected map[string]int, batch []ItemOpinion) error {
	if len(batch) == 0 {
		return fmt.Errorf("%w: at least one item opinion is required", ErrInvalidInput)
	}
	for _, op := range batch {
		switch op.Outcome {
		case models.OutcomePass, models.OutcomeFail, models.OutcomeNA:
		default:
			return ErrOutcomeInvalid
		}
		if op.Outcome == models.OutcomeFail && strings.TrimSpace(op.Reason) == "" {
			return ErrFailReasonRequired
		}
		if len(op.Reason) > 1000 {
			return fmt.Errorf("%w: reason too long (max 1000)", ErrInvalidInput)
		}
	}

	return s.db.Transaction(func(tx *gorm.DB) error {
		job, reviewers, found, err := s.loadJobLocked(tx, jobID)
		if err != nil {
			return err
		}
		if !found {
			return ErrNotFound
		}
		if job.Status == models.JobApproved {
			return ErrJobClosed
		}
		if !isAssignedReviewer(reviewerID, reviewers) {
			// The actor is either a job member without a reviewer seat
			// (designer/PM) or a complete outsider; the latter must not even
			// learn the job exists.
			if isMember(reviewerID, job, reviewers) {
				return ErrRoleDenied
			}
			return ErrForbidden
		}

		var round models.Round
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).
			Where("job_id = ? AND active_flag = 1", jobID).First(&round).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return ErrRoundInactive
			}
			return err
		}

		var items []models.RoundItem
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).
			Where("round_id = ?", round.ID).Find(&items).Error; err != nil {
			return err
		}
		itemByCode := make(map[string]models.RoundItem, len(items))
		for _, it := range items {
			itemByCode[it.Code] = it
		}

		// Lock this reviewer's opinion rows in a stable order alongside the
		// concurrent sign-off path to avoid deadlocks.
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).
			Where("round_id = ? AND reviewer_id = ?", round.ID, reviewerID).
			Find(&[]models.Opinion{}).Error; err != nil {
			return err
		}

		for _, op := range batch {
			item, ok := itemByCode[op.Code]
			if !ok {
				return fmt.Errorf("%w: %s", ErrUnknownItem, op.Code)
			}
			var existing models.Opinion
			findErr := tx.Where("round_id = ? AND reviewer_id = ? AND round_item_id = ?",
				round.ID, reviewerID, item.ID).First(&existing).Error
			switch {
			case errors.Is(findErr, gorm.ErrRecordNotFound):
				if exp, present := expected[op.Code]; present && exp != 0 {
					return ErrConflict
				}
				created := models.Opinion{
					RoundID:     round.ID,
					ReviewerID:  reviewerID,
					RoundItemID: item.ID,
					Outcome:     op.Outcome,
					Reason:      strings.TrimSpace(op.Reason),
					Version:     1,
				}
				if err := tx.Create(&created).Error; err != nil {
					return err
				}
			case findErr != nil:
				return findErr
			default:
				exp, present := expected[op.Code]
				if !present {
					// No expectation supplied for an existing opinion: refuse
					// rather than silently overwrite concurrent work.
					return fmt.Errorf("%w: item %s requires expected_version %d",
						ErrConflict, op.Code, existing.Version)
				}
				if exp != existing.Version {
					return fmt.Errorf("%w: item %s is at version %d, expected %d",
						ErrConflict, op.Code, existing.Version, exp)
				}
				if err := tx.Model(&existing).Updates(map[string]any{
					"outcome": op.Outcome,
					"reason":  strings.TrimSpace(op.Reason),
					"version": existing.Version + 1,
				}).Error; err != nil {
					return err
				}
			}
		}
		return nil
	})
}

// ---------------------------------------------------------------------------
// Approval
// ---------------------------------------------------------------------------

// Approve performs the final sign-off. It re-validates every rule under row
// locks so concurrent opinion edits, rival sign-offs and new revisions cannot
// slip past the checks:
//   - only the job's PM approves, and never the designer of their own job;
//   - every assigned reviewer has recorded an outcome for every snapshot item;
//   - no recorded outcome is "fail" (no stale failures, no missing verdicts).
func (s *Service) Approve(jobID, approverID uint) (*models.Approval, error) {
	var approval models.Approval
	err := s.db.Transaction(func(tx *gorm.DB) error {
		job, reviewers, found, err := s.loadJobLocked(tx, jobID)
		if err != nil {
			return err
		}
		if !found {
			return ErrNotFound
		}
		if approverID == job.DesignerID {
			return ErrSelfApproval
		}
		if approverID != job.PMID {
			if isMember(approverID, job, reviewers) {
				return ErrNotApprover
			}
			return ErrForbidden
		}
		if job.Status == models.JobApproved {
			return ErrJobClosed
		}

		var round models.Round
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).
			Where("job_id = ? AND active_flag = 1", jobID).First(&round).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return ErrRoundInactive
			}
			return err
		}

		// Lock all opinions of the round so concurrent upserts block until the
		// sign-off decision is committed.
		var opinions []models.Opinion
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).
			Where("round_id = ?", round.ID).Find(&opinions).Error; err != nil {
			return err
		}
		var items []models.RoundItem
		if err := tx.Where("round_id = ?", round.ID).Order("sort_order ASC, id ASC").Find(&items).Error; err != nil {
			return err
		}

		if err := validateSignOff(items, reviewers, opinions); err != nil {
			return err
		}

		now := time.Now().UTC()
		round.Status = models.RoundApproved
		round.ActiveFlag = nil
		round.ApprovedAt = &now
		if err := tx.Model(&round).Updates(map[string]any{
			"status":      round.Status,
			"active_flag": nil,
			"approved_at": now,
		}).Error; err != nil {
			return err
		}
		job.Status = models.JobApproved
		if err := tx.Model(job).Update("status", models.JobApproved).Error; err != nil {
			return err
		}

		basis, err := s.buildBasis(tx, job, reviewers, &round, items, opinions)
		if err != nil {
			return err
		}
		approval = models.Approval{
			RoundID:    round.ID,
			JobID:      jobID,
			ApproverID: approverID,
			Basis:      basis,
		}
		if err := tx.Create(&approval).Error; err != nil {
			return err
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &approval, nil
}

// validateSignOff encodes the blocking rules: complete coverage first, then
// zero failures.
func validateSignOff(items []models.RoundItem, reviewers []models.JobReviewer, opinions []models.Opinion) error {
	if len(items) == 0 || len(reviewers) == 0 {
		return ErrReviewersIncomplete
	}
	type key struct {
		reviewer uint
		item     uint
	}
	seen := make(map[key]models.Opinion, len(opinions))
	for _, o := range opinions {
		seen[key{o.ReviewerID, o.RoundItemID}] = o
	}
	for _, rev := range reviewers {
		for _, it := range items {
			o, ok := seen[key{rev.UserID, it.ID}]
			if !ok {
				return ErrPendingItems
			}
			if o.Outcome == models.OutcomeFail {
				return ErrFailedItems
			}
			if o.Outcome != models.OutcomePass && o.Outcome != models.OutcomeNA {
				return ErrOutcomeInvalid
			}
		}
	}
	return nil
}

// ---------------------------------------------------------------------------
// History & report
// ---------------------------------------------------------------------------

// RoundDetail is the per-version review history view.
type RoundDetail struct {
	Round       models.Round       `json:"round"`
	FileVersion models.FileVersion `json:"file_version"`
	Items       []models.RoundItem `json:"items"`
	Opinions    []OpinionView      `json:"opinions"`
	Approval    *models.Approval   `json:"approval,omitempty"`
}

// OpinionView joins an opinion with its reviewer and item code.
type OpinionView struct {
	models.Opinion
	ReviewerName string `json:"reviewer_name"`
	ItemCode     string `json:"item_code"`
}

// History returns the review history of one version (round snapshot, every
// opinion, approval if any). Only job members may read it.
func (s *Service) History(jobID, actorID uint, versionNumber int) (*RoundDetail, error) {
	job, reviewers, found, err := s.GetJob(jobID)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, ErrNotFound
	}
	if !isMember(actorID, job, reviewers) {
		return nil, ErrForbidden
	}
	round, err := s.RoundByVersion(jobID, versionNumber)
	if err != nil {
		return nil, err
	}
	return s.assembleRound(s.db, round)
}

// FullHistory lists every version's history for a job.
func (s *Service) FullHistory(jobID, actorID uint) ([]RoundDetail, error) {
	job, reviewers, found, err := s.GetJob(jobID)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, ErrNotFound
	}
	if !isMember(actorID, job, reviewers) {
		return nil, ErrForbidden
	}
	var rounds []models.Round
	if err := s.db.Where("job_id = ?", jobID).Order("version_number ASC").Find(&rounds).Error; err != nil {
		return nil, err
	}
	out := make([]RoundDetail, 0, len(rounds))
	for i := range rounds {
		d, err := s.assembleRound(s.db, &rounds[i])
		if err != nil {
			return nil, err
		}
		out = append(out, *d)
	}
	return out, nil
}

func (s *Service) assembleRound(gdb *gorm.DB, round *models.Round) (*RoundDetail, error) {
	d := &RoundDetail{Round: *round}
	if err := gdb.First(&d.FileVersion, round.VersionID).Error; err != nil {
		return nil, err
	}
	if err := gdb.Where("round_id = ?", round.ID).Order("sort_order ASC, id ASC").Find(&d.Items).Error; err != nil {
		return nil, err
	}
	var opinions []models.Opinion
	if err := gdb.Where("round_id = ?", round.ID).Find(&opinions).Error; err != nil {
		return nil, err
	}
	userNames := map[uint]string{}
	itemCodes := map[uint]string{}
	var users []models.User
	if err := gdb.Where("id IN ?", reviewerIDsOf(opinions)).Find(&users).Error; err != nil {
		return nil, err
	}
	for _, u := range users {
		userNames[u.ID] = u.Username
	}
	for _, it := range d.Items {
		itemCodes[it.ID] = it.Code
	}
	d.Opinions = make([]OpinionView, 0, len(opinions))
	for _, o := range opinions {
		d.Opinions = append(d.Opinions, OpinionView{
			Opinion:      o,
			ReviewerName: userNames[o.ReviewerID],
			ItemCode:     itemCodes[o.RoundItemID],
		})
	}
	sort.Slice(d.Opinions, func(i, j int) bool {
		if d.Opinions[i].ReviewerID != d.Opinions[j].ReviewerID {
			return d.Opinions[i].ReviewerID < d.Opinions[j].ReviewerID
		}
		return d.Opinions[i].RoundItemID < d.Opinions[j].RoundItemID
	})

	var approval models.Approval
	err := gdb.Where("round_id = ?", round.ID).First(&approval).Error
	switch {
	case errors.Is(err, gorm.ErrRecordNotFound):
	case err != nil:
		return nil, err
	default:
		d.Approval = &approval
	}
	return d, nil
}

// buildBasis freezes the human-readable sign-off basis: file summary, the
// checklist snapshot and every reviewer verdict.
func (s *Service) buildBasis(tx *gorm.DB, job *models.Job, reviewers []models.JobReviewer,
	round *models.Round, items []models.RoundItem, opinions []models.Opinion) (string, error) {
	var fv models.FileVersion
	if err := tx.First(&fv, round.VersionID).Error; err != nil {
		return "", err
	}
	var pm models.User
	if err := tx.First(&pm, job.PMID).Error; err != nil {
		return "", err
	}
	names := map[uint]string{pm.ID: pm.Username}
	ids := make([]uint, 0, len(reviewers))
	for _, r := range reviewers {
		ids = append(ids, r.UserID)
		names[r.UserID] = r.User.Username
	}
	if len(names) < len(reviewers)+1 {
		var us []models.User
		if err := tx.Where("id IN ?", ids).Find(&us).Error; err != nil {
			return "", err
		}
		for _, u := range us {
			names[u.ID] = u.Username
		}
	}

	byKey := map[[2]uint]models.Opinion{}
	for _, o := range opinions {
		byKey[[2]uint{o.ReviewerID, o.RoundItemID}] = o
	}

	var b strings.Builder
	fmt.Fprintf(&b, "Sign-off basis for job #%d %q\n", job.ID, job.Title)
	fmt.Fprintf(&b, "File version %d: %s (%s, %d bytes, sha256=%s)\n",
		fv.Version, fv.Filename, strings.ToUpper(fv.Kind), fv.Size, fv.SHA256)
	fmt.Fprintf(&b, "Checklist snapshot %s, %d items; approved by PM %s at %s\n",
		round.ChecklistVersion, len(items), names[job.PMID],
		round.ApprovedAt.UTC().Format(time.RFC3339))
	b.WriteString("All assigned reviewers completed every item; no failed outcomes.\n\n")
	for _, it := range items {
		fmt.Fprintf(&b, "- [%s] %s\n", it.Code, it.Description)
		for _, r := range reviewers {
			o := byKey[[2]uint{r.UserID, it.ID}]
			label := strings.ToUpper(o.Outcome)
			if o.Outcome == models.OutcomeNA {
				label = "N/A"
			}
			if o.Outcome == models.OutcomeFail {
				fmt.Fprintf(&b, "    %s: FAIL — %s\n", names[r.UserID], o.Reason)
			} else {
				fmt.Fprintf(&b, "    %s: %s\n", names[r.UserID], label)
			}
		}
	}
	return b.String(), nil
}

// ---------------------------------------------------------------------------
// Download
// ---------------------------------------------------------------------------

// OpenVersion authorizes the actor and returns the stored blob plus metadata.
func (s *Service) OpenVersion(jobID, actorID uint, versionNumber int) (io.ReadCloser, *models.FileVersion, error) {
	job, reviewers, found, err := s.GetJob(jobID)
	if err != nil {
		return nil, nil, err
	}
	if !found {
		return nil, nil, ErrNotFound
	}
	if !isMember(actorID, job, reviewers) {
		return nil, nil, ErrForbidden
	}
	v, err := s.GetVersion(jobID, versionNumber)
	if err != nil {
		return nil, nil, err
	}
	f, err := s.store.Open(v.StoragePath)
	if err != nil {
		return nil, nil, err
	}
	return f, v, nil
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func (s *Service) loadJobLocked(tx *gorm.DB, jobID uint) (*models.Job, []models.JobReviewer, bool, error) {
	var job models.Job
	if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).First(&job, jobID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, nil, false, nil
		}
		return nil, nil, false, err
	}
	var reviewers []models.JobReviewer
	if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).
		Preload("User").
		Where("job_id = ?", jobID).Find(&reviewers).Error; err != nil {
		return nil, nil, false, err
	}
	return &job, reviewers, true, nil
}

func isAssignedReviewer(userID uint, reviewers []models.JobReviewer) bool {
	for _, r := range reviewers {
		if r.UserID == userID {
			return true
		}
	}
	return false
}

func isMember(userID uint, job *models.Job, reviewers []models.JobReviewer) bool {
	if userID == job.DesignerID || userID == job.PMID {
		return true
	}
	return isAssignedReviewer(userID, reviewers)
}

// canContribute: designer or PM may upload/revise files.
func canContribute(userID uint, job *models.Job, reviewers []models.JobReviewer) bool {
	return userID == job.DesignerID || userID == job.PMID
}

func reviewerIDsOf(os []models.Opinion) []uint {
	set := map[uint]struct{}{}
	for _, o := range os {
		set[o.ReviewerID] = struct{}{}
	}
	out := make([]uint, 0, len(set))
	for id := range set {
		out = append(out, id)
	}
	return out
}

func dedupe(in []uint) []uint {
	seen := map[uint]struct{}{}
	out := make([]uint, 0, len(in))
	for _, v := range in {
		if v == 0 {
			continue
		}
		if _, ok := seen[v]; ok {
			continue
		}
		seen[v] = struct{}{}
		out = append(out, v)
	}
	return out
}

func boolPtr(i int) *int { return &i }

func mapStoreErr(err error) error {
	switch {
	case errors.Is(err, storage.ErrTooLarge):
		return ErrFileTooLarge
	case errors.Is(err, storage.ErrUnsupportedKind):
		return ErrFileType
	default:
		return err
	}
}

// sanitizeFilename keeps a client-supplied display name safe for storage
// metadata (it never influences the on-disk path).
func sanitizeFilename(name string) string {
	name = strings.TrimSpace(name)
	name = strings.ReplaceAll(name, "\x00", "")
	name = strings.ReplaceAll(name, "/", "_")
	name = strings.ReplaceAll(name, "\\", "_")
	if name == "" {
		name = "upload"
	}
	if len(name) > 255 {
		name = name[:255]
	}
	return name
}

package service

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"gorm.io/gorm"

	"proofcycle/internal/model"
)

// SubmitOpinions upserts one reviewer's verdicts for one explicit file
// version's round. Every item carries the opinion row version the client last
// saw; a stale or missing expectation is rejected with 409 rather than silently
// overwriting another update.
func (s *Service) SubmitOpinions(ctx context.Context, caller *model.User, jobID int64, in SubmitOpinionsInput) ([]model.Opinion, *model.ReviewRound, error) {
	if caller.Role != model.RoleReviewer {
		return nil, nil, errForbidden("only reviewers can submit review opinions")
	}
	if in.Version <= 0 {
		return nil, nil, errBadRequest("version is required")
	}
	if len(in.Items) == 0 {
		return nil, nil, errBadRequest("at least one review item is required")
	}
	type parsed struct {
		code    string
		verdict string
		reason  string
		expect  int
	}
	reqs := make([]parsed, 0, len(in.Items))
	for i, it := range in.Items {
		code := strings.TrimSpace(it.ChecklistCode)
		if code == "" {
			return nil, nil, errBadRequest(fmt.Sprintf("item %d: checklist_code is required", i+1))
		}
		switch it.Verdict {
		case model.VerdictPass, model.VerdictFail, model.VerdictNA:
		default:
			return nil, nil, errBadRequest(fmt.Sprintf("item %d: verdict must be pass, fail or na", i+1))
		}
		reason := strings.TrimSpace(it.Reason)
		if it.Verdict == model.VerdictFail && reason == "" {
			return nil, nil, errUnprocessable("reason_required",
				fmt.Sprintf("item %s: a failed item requires a reason", code))
		}
		if len(reason) > 1000 {
			return nil, nil, errBadRequest(fmt.Sprintf("item %s: reason too long (max 1000)", code))
		}
		if it.ExpectedVersion < 0 {
			return nil, nil, errBadRequest(fmt.Sprintf("item %s: expected_version must be 0 or positive", code))
		}
		reqs = append(reqs, parsed{code: code, verdict: it.Verdict, reason: reason, expect: it.ExpectedVersion})
	}

	var saved []model.Opinion
	var round model.ReviewRound
	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		job, lerr := lockJob(ctx, tx, jobID)
		if lerr != nil {
			return lerr
		}
		var assigned model.JobReviewer
		aErr := tx.Where("job_id = ? AND reviewer_id = ?", jobID, caller.ID).First(&assigned).Error
		if errors.Is(aErr, gorm.ErrRecordNotFound) {
			return errForbidden("you are not a designated reviewer of this job")
		}
		if aErr != nil {
			return aErr
		}
		rErr := tx.Where("job_id = ? AND version = ?", jobID, in.Version).First(&round).Error
		if errors.Is(rErr, gorm.ErrRecordNotFound) {
			return errNotFound("review round for that version does not exist")
		}
		if rErr != nil {
			return rErr
		}
		switch round.Status {
		case model.RoundStatusOpen:
			// current round: opinions accepted
		case model.RoundStatusSuperseded:
			return errUnprocessable("version_superseded",
				"this version has been superseded by a newer revision; review the current version instead")
		case model.RoundStatusApproved:
			return errConflict("this version is already approved and its review is closed")
		default:
			return errConflict("round is not open for review")
		}
		_ = job

		var items []model.ChecklistItem
		if lerr = tx.Where("round_id = ?", round.ID).Order("item_order ASC").Find(&items).Error; lerr != nil {
			return lerr
		}
		byCode := map[string]model.ChecklistItem{}
		for _, it := range items {
			byCode[it.Code] = it
		}
		now := time.Now().UTC()
		for _, rq := range reqs {
			ci, ok := byCode[rq.code]
			if !ok {
				return errBadRequest("checklist code not present in this version's snapshot: " + rq.code)
			}
			var existing model.Opinion
			qErr := tx.Where("round_id = ? AND reviewer_id = ? AND checklist_item_id = ?",
				round.ID, caller.ID, ci.ID).First(&existing).Error
			if errors.Is(qErr, gorm.ErrRecordNotFound) {
				if rq.expect != 0 {
					return errConflict(fmt.Sprintf("opinion for %s does not exist; expected_version must be 0", ci.Code))
				}
				op := model.Opinion{
					RoundID: round.ID, ReviewerID: caller.ID, ChecklistItemID: ci.ID,
					Verdict: rq.verdict, Reason: rq.reason, RowVersion: 1,
					CreatedAt: now, UpdatedAt: now,
				}
				if cErr := tx.Create(&op).Error; cErr != nil {
					if isDuplicate(cErr) {
						return errConflict("opinion changed concurrently; refetch and retry")
					}
					return cErr
				}
				saved = append(saved, op)
				continue
			}
			if qErr != nil {
				return qErr
			}
			if rq.expect != existing.RowVersion {
				return errConflict(fmt.Sprintf(
					"opinion for %s was updated by another request (current version %d, you sent %d)",
					ci.Code, existing.RowVersion, rq.expect))
			}
			existing.Verdict = rq.verdict
			existing.Reason = rq.reason
			existing.RowVersion++
			existing.UpdatedAt = now
			if uErr := tx.Model(&model.Opinion{}).Where("id = ? AND row_version = ?", existing.ID, rq.expect).
				Updates(map[string]any{
					"verdict": existing.Verdict, "reason": existing.Reason,
					"row_version": existing.RowVersion, "updated_at": now,
				}).Error; uErr != nil {
				return uErr
			}
			saved = append(saved, existing)
		}
		return nil
	})
	if err != nil {
		return nil, nil, err
	}
	return saved, &round, nil
}

// approvalState evaluates the CURRENT open round against the sign-off rules:
// every designated reviewer must have answered every checklist item, and no
// answer may be "fail".
type approvalState struct {
	Round         model.ReviewRound
	Reviewers     []model.JobReviewer
	Items         []model.ChecklistItem
	Opinions      []model.Opinion
	MissingByUser map[int64][]string // reviewer -> codes not answered
	FailedByUser  map[int64][]string // reviewer -> codes failed
}

func (s *Service) evaluateOpenRound(ctx context.Context, tx *gorm.DB, job *model.Job) (*approvalState, error) {
	var round model.ReviewRound
	err := tx.WithContext(ctx).Where("job_id = ? AND version = ? AND status = ?",
		job.ID, job.CurrentVersion, model.RoundStatusOpen).First(&round).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, errUnprocessable("no_open_round", "the current version has no open review round")
	}
	if err != nil {
		return nil, err
	}
	var reviewers []model.JobReviewer
	if err = tx.Where("job_id = ?", job.ID).Find(&reviewers).Error; err != nil {
		return nil, err
	}
	var items []model.ChecklistItem
	if err = tx.Where("round_id = ?", round.ID).Order("item_order ASC").Find(&items).Error; err != nil {
		return nil, err
	}
	var opinions []model.Opinion
	if err = tx.Where("round_id = ?", round.ID).Find(&opinions).Error; err != nil {
		return nil, err
	}
	answered := map[int64]map[int64]model.Opinion{} // reviewer -> item -> opinion
	for _, op := range opinions {
		m, ok := answered[op.ReviewerID]
		if !ok {
			m = map[int64]model.Opinion{}
			answered[op.ReviewerID] = m
		}
		m[op.ChecklistItemID] = op
	}
	st := &approvalState{
		Round: round, Reviewers: reviewers, Items: items, Opinions: opinions,
		MissingByUser: map[int64][]string{}, FailedByUser: map[int64][]string{},
	}
	for _, rv := range reviewers {
		ops := answered[rv.ReviewerID]
		for _, it := range items {
			op, ok := ops[it.ID]
			if !ok {
				st.MissingByUser[rv.ReviewerID] = append(st.MissingByUser[rv.ReviewerID], it.Code)
				continue
			}
			if op.Verdict == model.VerdictFail {
				st.FailedByUser[rv.ReviewerID] = append(st.FailedByUser[rv.ReviewerID], it.Code)
			}
		}
	}
	return st, nil
}

// SignOff finalizes the current version. Only the job's PM may sign, and the
// designer of a job can never be its PM. The job-row lock makes concurrent
// sign-offs, opinion edits and new-revision uploads serialize, so none of them
// can slip a failing or incomplete state through approval.
func (s *Service) SignOff(ctx context.Context, caller *model.User, jobID int64) (*model.Signoff, *approvalState, error) {
	if caller.Role != model.RolePM {
		return nil, nil, errForbidden("only the project manager can sign off")
	}
	var result *model.Signoff
	var state *approvalState
	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		job, lerr := lockJob(ctx, tx, jobID)
		if lerr != nil {
			return lerr
		}
		if job.DesignerID == caller.ID {
			// Defense in depth: assignment already forbids PM==designer.
			return errForbidden("the designer cannot approve their own job")
		}
		if job.PMID != caller.ID {
			return errForbidden("you are not the project manager of this job")
		}
		if job.Status == model.JobStatusApproved {
			return errConflict("job is already approved")
		}
		st, eErr := s.evaluateOpenRound(ctx, tx, job)
		if eErr != nil {
			return eErr
		}
		state = st
		if len(st.MissingByUser) > 0 || len(st.FailedByUser) > 0 {
			return s.blockingError(ctx, tx, job, st)
		}

		basis := s.buildBasis(tx, job, st)
		now := time.Now().UTC()
		if uErr := tx.Model(&model.ReviewRound{}).Where("id = ?", st.Round.ID).
			Updates(map[string]any{
				"status": model.RoundStatusApproved, "signoff_by": caller.ID,
				"signoff_at": now, "signoff_basis": basis,
			}).Error; uErr != nil {
			return uErr
		}
		if uErr := tx.Model(&model.FileVersion{}).Where("id = ?", st.Round.FileVerID).
			Update("status", model.VersionStatusApproved).Error; uErr != nil {
			return uErr
		}
		so := model.Signoff{
			RoundID: st.Round.ID, JobID: job.ID, Version: job.CurrentVersion,
			PMUserID: caller.ID, Basis: basis, CreatedAt: now,
		}
		if cErr := tx.Create(&so).Error; cErr != nil {
			if isDuplicate(cErr) {
				return errConflict("job is already approved")
			}
			return cErr
		}
		if uErr := tx.Model(&model.Job{}).Where("id = ? AND status = ?", job.ID, model.JobStatusInReview).
			Update("status", model.JobStatusApproved).Error; uErr != nil {
			return uErr
		}
		result = &so
		return nil
	})
	if err != nil {
		return nil, state, err
	}
	return result, state, nil
}

// blockingDetail is the JSON body returned when sign-off is rejected.
type blockingDetail struct {
	Reason  string              `json:"reason"`
	Missing map[string][]string `json:"pending_by_reviewer,omitempty"`
	Failed  map[string][]string `json:"failed_by_reviewer,omitempty"`
}

func (s *Service) blockingError(ctx context.Context, tx *gorm.DB, job *model.Job, st *approvalState) error {
	ids := make([]int64, 0, len(st.Reviewers))
	for _, rv := range st.Reviewers {
		ids = append(ids, rv.ReviewerID)
	}
	users, _ := s.loadUsers(ctx, tx, ids)
	nameOf := func(id int64) string {
		if u, ok := users[id]; ok {
			return u.Name
		}
		return fmt.Sprintf("user#%d", id)
	}
	d := blockingDetail{
		Reason:  "all designated reviewers must complete every checklist item with no failures",
		Missing: map[string][]string{},
		Failed:  map[string][]string{},
	}
	for id, codes := range st.MissingByUser {
		d.Missing[nameOf(id)] = codes
	}
	for id, codes := range st.FailedByUser {
		d.Failed[nameOf(id)] = codes
	}
	if len(d.Missing) == 0 {
		d.Missing = nil
	}
	if len(d.Failed) == 0 {
		d.Failed = nil
	}
	return errUnprocessable("signoff_blocked", "sign-off blocked by pending or failed checklist items").withDetail(d)
}

// buildBasis writes the human-auditable approval evidence: completion matrix
// plus explicit confirmation that no fail verdict exists.
func (s *Service) buildBasis(tx *gorm.DB, job *model.Job, st *approvalState) string {
	users := map[int64]model.User{}
	var us []model.User
	ids := []int64{job.PMID}
	for _, rv := range st.Reviewers {
		ids = append(ids, rv.ReviewerID)
	}
	_ = tx.Where("id IN ?", ids).Find(&us).Error
	for _, u := range us {
		users[u.ID] = u
	}
	var b strings.Builder
	fmt.Fprintf(&b, "Sign-off basis for job %d version %d.\n", job.ID, job.CurrentVersion)
	fmt.Fprintf(&b, "Designated reviewers (%d) each answered all %d checklist items; no item was failed.\n",
		len(st.Reviewers), len(st.Items))
	for _, rv := range st.Reviewers {
		name := users[rv.ReviewerID].Name
		pass, na := 0, 0
		for _, op := range st.Opinions {
			if op.ReviewerID != rv.ReviewerID {
				continue
			}
			switch op.Verdict {
			case model.VerdictPass:
				pass++
			case model.VerdictNA:
				na++
			}
		}
		fmt.Fprintf(&b, "- reviewer %s: %d pass, %d n/a, 0 fail, 0 pending\n", name, pass, na)
	}
	return b.String()
}

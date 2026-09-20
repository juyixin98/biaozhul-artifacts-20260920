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

// JobDetail returns the job plus every review round (all file versions) for an
// assigned participant only. This backs "review history by version".
func (s *Service) JobDetail(ctx context.Context, caller *model.User, jobID int64) (*JobDetailDTO, error) {
	job, users, err := s.authorizeParticipant(ctx, caller, jobID)
	if err != nil {
		return nil, err
	}
	var reviewers []model.JobReviewer
	if err = s.db.WithContext(ctx).Where("job_id = ?", jobID).Find(&reviewers).Error; err != nil {
		return nil, err
	}
	dto := JobDetailDTO{JobDTO: jobToDTO(job, users, reviewers)}

	var versions []model.FileVersion
	if err = s.db.WithContext(ctx).Where("job_id = ?", jobID).
		Order("version ASC").Find(&versions).Error; err != nil {
		return nil, err
	}
	verByID := map[int64]model.FileVersion{}
	for _, v := range versions {
		verByID[v.ID] = v
	}
	var rounds []model.ReviewRound
	if err = s.db.WithContext(ctx).Where("job_id = ?", jobID).
		Order("version ASC").Find(&rounds).Error; err != nil {
		return nil, err
	}
	for _, rd := range rounds {
		view, err := s.roundView(ctx, rd, verByID[rd.FileVerID], users)
		if err != nil {
			return nil, err
		}
		dto.Rounds = append(dto.Rounds, *view)
	}
	return &dto, nil
}

// VersionHistory returns one version's round, checklist snapshot and opinions.
func (s *Service) VersionHistory(ctx context.Context, caller *model.User, jobID int64, version int) (*RoundView, error) {
	_, _, err := s.authorizeParticipant(ctx, caller, jobID)
	if err != nil {
		return nil, err
	}
	var round model.ReviewRound
	err = s.db.WithContext(ctx).Where("job_id = ? AND version = ?", jobID, version).First(&round).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, errNotFound("no review round for that version")
	}
	if err != nil {
		return nil, err
	}
	var fv model.FileVersion
	if err = s.db.WithContext(ctx).
		Where("job_id = ? AND version = ?", jobID, version).
		First(&fv).Error; err != nil && !errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, err
	}
	var reviewers []model.JobReviewer
	if err = s.db.WithContext(ctx).Where("job_id = ?", jobID).Find(&reviewers).Error; err != nil {
		return nil, err
	}
	ids := []int64{}
	for _, r := range reviewers {
		ids = append(ids, r.ReviewerID)
	}
	users, err := s.loadUsers(ctx, s.db, ids)
	if err != nil {
		return nil, err
	}
	return s.roundView(ctx, round, fv, users)
}

func (s *Service) roundView(ctx context.Context, round model.ReviewRound, fv model.FileVersion, users map[int64]model.User) (*RoundView, error) {
	var items []model.ChecklistItem
	if err := s.db.WithContext(ctx).Where("round_id = ?", round.ID).
		Order("item_order ASC").Find(&items).Error; err != nil {
		return nil, err
	}
	var ops []model.Opinion
	if err := s.db.WithContext(ctx).Where("round_id = ?", round.ID).
		Order("id ASC").Find(&ops).Error; err != nil {
		return nil, err
	}
	opByItem := map[int64][]model.Opinion{}
	for _, op := range ops {
		opByItem[op.ChecklistItemID] = append(opByItem[op.ChecklistItemID], op)
	}
	view := &RoundView{
		Version: round.Version,
		Status:  round.Status,
		File: VersionDTO{
			Version: fv.Version, FileName: fv.FileName, SHA256: fv.SHA256,
			SizeBytes: fv.SizeBytes, MimeType: fv.MimeType, Status: fv.Status,
			UploadedBy: fv.UploadedBy, CreatedAt: fv.CreatedAt,
		},
		SignoffBasis: round.SignoffBasis,
		CreatedAt:    round.CreatedAt,
	}
	if round.SignoffBy != nil {
		view.SignoffBy = round.SignoffBy
		view.SignoffAt = round.SignoffAt
		if u, ok := users[*round.SignoffBy]; ok {
			view.SignoffByName = u.Name
		}
	}
	pending := map[string]bool{}
	failed := map[string]bool{}
	for _, it := range items {
		cv := ChecklistItemView{Code: it.Code, Description: it.Description}
		for _, op := range opByItem[it.ID] {
			name := users[op.ReviewerID].Name
			cv.Opinions = append(cv.Opinions, OpinionView{
				ReviewerID: op.ReviewerID, ReviewerName: name, Verdict: op.Verdict,
				Reason: op.Reason, RowVersion: op.RowVersion, UpdatedAt: op.UpdatedAt,
			})
			if op.Verdict == model.VerdictFail {
				failed[it.Code] = true
			}
		}
		if len(cv.Opinions) == 0 {
			pending[it.Code] = true
		}
		view.Checklist = append(view.Checklist, cv)
	}
	// Pending/failed aggregates only matter while the round is open.
	if round.Status == model.RoundStatusOpen {
		for code := range pending {
			view.PendingItems = append(view.PendingItems, code)
		}
		for code := range failed {
			view.FailedItems = append(view.FailedItems, code)
		}
	}
	return view, nil
}

func jobToDTO(job *model.Job, users map[int64]model.User, reviewers []model.JobReviewer) JobDTO {
	dto := JobDTO{
		ID: job.ID, Name: job.Name, Status: job.Status, CurrentVersion: job.CurrentVersion,
		DesignerID: job.DesignerID, PMID: job.PMID,
		CreatedAt: job.CreatedAt, UpdatedAt: job.UpdatedAt,
	}
	if u, ok := users[job.DesignerID]; ok {
		dto.DesignerName = u.Name
	}
	if u, ok := users[job.PMID]; ok {
		dto.PMName = u.Name
	}
	for _, r := range reviewers {
		dto.ReviewerIDs = append(dto.ReviewerIDs, r.ReviewerID)
	}
	return dto
}

// Report renders the exportable review report for one version: file summary,
// checklist snapshot with all opinions, and the sign-off basis. JSON or
// Markdown. Authorization is identical to history (participants only).
func (s *Service) Report(ctx context.Context, caller *model.User, jobID int64, version int, markdown bool) (*JobDetailDTO, string, error) {
	detail, err := s.JobDetail(ctx, caller, jobID)
	if err != nil {
		return nil, "", err
	}
	var chosen *RoundView
	for i := range detail.Rounds {
		if detail.Rounds[i].Version == version {
			chosen = &detail.Rounds[i]
		}
	}
	if chosen == nil {
		return nil, "", errNotFound("no review round for that version")
	}
	if !markdown {
		return detail, "", nil
	}
	return detail, renderMarkdown(detail, chosen), nil
}

func renderMarkdown(d *JobDetailDTO, r *RoundView) string {
	var b strings.Builder
	fmt.Fprintf(&b, "# ProofCycle Report — Job %d: %s\n\n", d.ID, d.Name)
	fmt.Fprintf(&b, "- Designer: %s\n- Project manager: %s\n- Version: %d\n- Round status: %s\n\n",
		d.DesignerName, d.PMName, r.Version, r.Status)
	fmt.Fprintf(&b, "## File summary\n\n")
	fmt.Fprintf(&b, "| Field | Value |\n|---|---|\n")
	fmt.Fprintf(&b, "| File name | %s |\n", blank(r.File.FileName))
	fmt.Fprintf(&b, "| Type | %s |\n", r.File.MimeType)
	fmt.Fprintf(&b, "| Size (bytes) | %d |\n", r.File.SizeBytes)
	fmt.Fprintf(&b, "| SHA-256 | `%s` |\n", r.File.SHA256)
	fmt.Fprintf(&b, "| Uploaded at | %s |\n\n", r.File.CreatedAt.Format(time.RFC3339))

	fmt.Fprintf(&b, "## Checklist snapshot and opinions\n\n")
	for _, it := range r.Checklist {
		fmt.Fprintf(&b, "### [%s] %s\n\n", it.Code, it.Description)
		if len(it.Opinions) == 0 {
			fmt.Fprintf(&b, "- _no opinions recorded (pending)_\n\n")
			continue
		}
		for _, op := range it.Opinions {
			line := fmt.Sprintf("- **%s**: %s", op.ReviewerName, op.Verdict)
			if op.Reason != "" {
				line += " — " + op.Reason
			}
			b.WriteString(line + "\n")
		}
		b.WriteString("\n")
	}
	fmt.Fprintf(&b, "## Sign-off\n\n")
	if r.Status == model.RoundStatusApproved {
		fmt.Fprintf(&b, "Approved by %s at %s.\n\n", blank(r.SignoffByName),
			r.SignoffAt.Format(time.RFC3339))
		b.WriteString("```\n" + r.SignoffBasis + "\n```\n")
	} else {
		fmt.Fprintf(&b, "Not approved (round status: %s).\n", r.Status)
		if len(r.PendingItems) > 0 {
			fmt.Fprintf(&b, "Pending items: %s\n", strings.Join(r.PendingItems, ", "))
		}
		if len(r.FailedItems) > 0 {
			fmt.Fprintf(&b, "Failed items: %s\n", strings.Join(r.FailedItems, ", "))
		}
	}
	return b.String()
}

func blank(s string) string {
	if strings.TrimSpace(s) == "" {
		return "—"
	}
	return s
}

package service

import (
	"fmt"
	"strings"

	"proofcycle/internal/models"
)

// Report produces the export for a job version: file summary, checklist
// snapshot, every opinion and the sign-off basis. Non-members get
// ErrForbidden before any data is assembled, so reports never leak
// unauthorized jobs. ver == 0 means the latest version.
func (s *Service) Report(jobID, actorID uint, ver int) (string, *models.Job, *RoundDetail, error) {
	job, reviewers, found, err := s.GetJob(jobID)
	if err != nil {
		return "", nil, nil, err
	}
	if !found {
		return "", nil, nil, ErrNotFound
	}
	if !isMember(actorID, job, reviewers) {
		return "", nil, nil, ErrForbidden
	}

	if ver == 0 {
		v, err := s.latestVersion(jobID)
		if err != nil {
			return "", nil, nil, err
		}
		ver = v.Version
	}
	detail, err := s.History(jobID, actorID, ver)
	if err != nil {
		return "", nil, nil, err
	}

	var b strings.Builder
	fmt.Fprintf(&b, "ProofCycle Review Report\n")
	fmt.Fprintf(&b, "========================\n")
	fmt.Fprintf(&b, "Job #%d: %s\n", job.ID, job.Title)
	if job.Description != "" {
		fmt.Fprintf(&b, "Description: %s\n", job.Description)
	}
	designerName, pmName := s.twoNames(job.DesignerID, job.PMID)
	fmt.Fprintf(&b, "Designer: %s | Project manager: %s | Job status: %s\n",
		designerName, pmName, job.Status)
	rv := make([]string, 0, len(reviewers))
	for _, r := range reviewers {
		rv = append(rv, r.User.Username)
	}
	fmt.Fprintf(&b, "Assigned reviewers (%d): %s\n\n", len(rv), strings.Join(rv, ", "))

	fv := detail.FileVersion
	fmt.Fprintf(&b, "File summary — version %d\n", fv.Version)
	fmt.Fprintf(&b, "  Original filename : %s\n", fv.Filename)
	fmt.Fprintf(&b, "  Type              : %s (local file)\n", strings.ToUpper(fv.Kind))
	fmt.Fprintf(&b, "  Size              : %d bytes\n", fv.Size)
	fmt.Fprintf(&b, "  SHA-256           : %s\n", fv.SHA256)
	fmt.Fprintf(&b, "  Uploaded at       : %s\n\n", fv.CreatedAt.UTC().Format("2006-01-02T15:04:05Z"))

	fmt.Fprintf(&b, "Round %d — checklist snapshot %s, status: %s\n",
		detail.Round.VersionNumber, detail.Round.ChecklistVersion, detail.Round.Status)

	opinionsByItem := map[uint][]OpinionView{}
	for _, o := range detail.Opinions {
		opinionsByItem[o.RoundItemID] = append(opinionsByItem[o.RoundItemID], o)
	}

	pending, failed := 0, 0
	for _, it := range detail.Items {
		fmt.Fprintf(&b, "\n[%s] %s\n", it.Code, it.Description)
		ops := opinionsByItem[it.ID]
		if len(ops) == 0 {
			b.WriteString("  (no opinions recorded)\n")
			pending += len(reviewers)
			continue
		}
		verdictByReviewer := map[uint]string{}
		for _, o := range ops {
			line := "  " + o.ReviewerName + ": " + strings.ToUpper(o.Outcome)
			if o.Outcome == models.OutcomeNA {
				line = "  " + o.ReviewerName + ": N/A"
			}
			if o.Reason != "" {
				line += " — " + o.Reason
			}
			b.WriteString(line + "\n")
			verdictByReviewer[o.ReviewerID] = o.Outcome
		}
		for _, r := range reviewers {
			v, ok := verdictByReviewer[r.UserID]
			if !ok {
				pending++
			} else if v == models.OutcomeFail {
				failed++
			}
		}
	}

	b.WriteString("\n------------------------\nSign-off basis\n------------------------\n")
	switch {
	case detail.Approval != nil:
		approver, _ := s.twoNames(detail.Approval.ApproverID, 0)
		fmt.Fprintf(&b, "APPROVED by %s at %s.\n", approver,
			detail.Approval.CreatedAt.UTC().Format("2006-01-02T15:04:05Z"))
		b.WriteString("All assigned reviewers completed every checklist item and no failure was open.\n")
		b.WriteString("\nFrozen basis:\n")
		b.WriteString(detail.Approval.Basis)
	case detail.Round.Status == models.RoundSuperseded:
		b.WriteString("NOT APPROVED — this version was superseded by a newer revision. Its opinions\n")
		b.WriteString("remain on record but cannot serve as approval basis for the new version.\n")
	default:
		fmt.Fprintf(&b, "NOT APPROVED — pending reviewer-item verdicts: %d, failed verdicts: %d.\n", pending, failed)
		b.WriteString("Approval requires every assigned reviewer to complete every item with no failure.\n")
	}

	return b.String(), job, detail, nil
}

func (s *Service) latestVersion(jobID uint) (*models.FileVersion, error) {
	var v models.FileVersion
	err := s.db.Where("job_id = ?", jobID).Order("version DESC").First(&v).Error
	if err != nil {
		return nil, err
	}
	return &v, nil
}

// twoNames resolves up to two user IDs to usernames (empty string if zero).
func (s *Service) twoNames(a, b uint) (string, string) {
	ids := make([]uint, 0, 2)
	if a != 0 {
		ids = append(ids, a)
	}
	if b != 0 {
		ids = append(ids, b)
	}
	byID := map[uint]string{}
	if len(ids) > 0 {
		var users []models.User
		if err := s.db.Where("id IN ?", ids).Find(&users).Error; err != nil {
			return "", ""
		}
		for _, u := range users {
			byID[u.ID] = u.Username
		}
	}
	return byID[a], byID[b]
}

package service

import (
	"errors"
	"fmt"
	"strings"
	"time"

	"gorm.io/gorm"

	"proofcycle/internal/domain"
)

// ReportService 生成按版本的审查报告（JSON 结构 + Markdown 导出）。
type ReportService struct {
	db *gorm.DB
}

// ReportItem 清单中单个检查项及所有审查员的结论。
type ReportItem struct {
	OrderNo  int             `json:"order_no"`
	Code     string          `json:"code"`
	Text     string          `json:"text"`
	Findings []ReportFinding `json:"findings"`
}

// ReportFinding 一名审查员对一项的结论。
type ReportFinding struct {
	ReviewerID string            `json:"reviewer_id"`
	Result     domain.ItemResult `json:"result"`
	FailReason string            `json:"fail_reason,omitempty"`
}

// Report 单版本审查报告。
type Report struct {
	Job          ReportJob       `json:"job"`
	File         ReportFile      `json:"file"`
	Checklist    ReportChecklist `json:"checklist"`
	ReviewStatus string          `json:"review_status"` // 完成情况
	Items        []ReportItem    `json:"items"`
	Approval     *ReportApproval `json:"approval,omitempty"`
	GeneratedAt  time.Time       `json:"generated_at"`
}

// ReportJob 报告中的作业摘要。
type ReportJob struct {
	ID         string `json:"id"`
	Name       string `json:"name"`
	DesignerID string `json:"designer_id"`
	PMID       string `json:"pm_id"`
	Status     string `json:"status"`
	CurrentVer int    `json:"current_version_no"`
}

// ReportFile 文件摘要（报告必须包含）。
type ReportFile struct {
	VersionNo   int       `json:"version_no"`
	FileName    string    `json:"file_name"`
	ContentType string    `json:"content_type"`
	SizeBytes   int64     `json:"size_bytes"`
	SHA256      string    `json:"sha256"`
	UploadedBy  string    `json:"uploaded_by"`
	UploadedAt  time.Time `json:"uploaded_at"`
}

// ReportChecklist 报告中的清单摘要。
type ReportChecklist struct {
	Name          string `json:"name"`
	SourceVersion int    `json:"source_version"`
	TotalItems    int    `json:"total_items"`
}

// ReportApproval 签核依据（报告必须包含）。
type ReportApproval struct {
	ApproverID string    `json:"approver_id"`
	Basis      string    `json:"basis"`
	ApprovedAt time.Time `json:"approved_at"`
}

// Build 生成某版本报告。
func (s *ReportService) Build(jobID string, versionNo int) (*Report, error) {
	var job domain.Job
	if err := s.db.First(&job, "id = ?", jobID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	var version domain.FileVersion
	err := s.db.First(&version, "job_id = ? AND version_no = ?", jobID, versionNo).Error
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}

	var snap domain.ChecklistSnapshot
	if err := s.db.First(&snap, "id = ?", version.SnapshotID).Error; err != nil {
		return nil, err
	}
	var snapItems []domain.ChecklistSnapshotItem
	if err := s.db.Where("snapshot_id = ?", snap.ID).Order("order_no").Find(&snapItems).Error; err != nil {
		return nil, err
	}

	var jobReviewers []domain.JobReviewer
	if err := s.db.Where("job_id = ?", jobID).Order("created_at").Find(&jobReviewers).Error; err != nil {
		return nil, err
	}
	var reviews []domain.Review
	if err := s.db.Where("version_id = ?", version.ID).Find(&reviews).Error; err != nil {
		return nil, err
	}
	var reviewItems []domain.ReviewItem
	if err := s.db.Where("review_id IN ?", reviewIDs(reviews)).Find(&reviewItems).Error; err != nil {
		return nil, err
	}

	// snapshotItemID -> reviewer findings
	bySnap := make(map[string][]ReportFinding)
	reviewerByReview := make(map[string]string, len(reviews))
	for _, r := range reviews {
		reviewerByReview[r.ID] = r.ReviewerID
	}
	for _, ri := range reviewItems {
		bySnap[ri.SnapshotItemID] = append(bySnap[ri.SnapshotItemID], ReportFinding{
			ReviewerID: reviewerByReview[ri.ReviewID],
			Result:     ri.Result,
			FailReason: ri.FailReason,
		})
	}

	items := make([]ReportItem, 0, len(snapItems))
	hasFail, hasIncomplete := false, false
	for _, si := range snapItems {
		f := bySnap[si.ID]
		items = append(items, ReportItem{OrderNo: si.OrderNo, Code: si.Code, Text: si.Text, Findings: f})
		for _, x := range f {
			if x.Result == domain.ResultFail {
				hasFail = true
			}
		}
	}
	if len(reviews) < len(jobReviewers) {
		hasIncomplete = true
	}

	rep := &Report{
		Job: ReportJob{
			ID: job.ID, Name: job.Name, DesignerID: job.DesignerID, PMID: job.PMID,
			Status: string(job.Status), CurrentVer: job.CurrentVersionNo,
		},
		File: ReportFile{
			VersionNo: version.VersionNo, FileName: version.FileName,
			ContentType: version.ContentType, SizeBytes: version.SizeBytes,
			SHA256: version.SHA256, UploadedBy: version.UploadedByID, UploadedAt: version.CreatedAt,
		},
		Checklist: ReportChecklist{
			Name: snap.ChecklistName, SourceVersion: snap.SourceVersion, TotalItems: len(snapItems),
		},
		Items:       items,
		GeneratedAt: time.Now().UTC(),
	}
	switch {
	case hasIncomplete:
		rep.ReviewStatus = "incomplete_reviews"
	case hasFail:
		rep.ReviewStatus = "has_failed_items"
	default:
		rep.ReviewStatus = "ready_for_approval"
	}

	var approval domain.Approval
	if err := s.db.First(&approval, "version_id = ?", version.ID).Error; err == nil {
		rep.Approval = &ReportApproval{
			ApproverID: approval.ApproverID, Basis: approval.Basis, ApprovedAt: approval.CreatedAt,
		}
	} else if !errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, err
	}
	return rep, nil
}

// RenderMarkdown 把报告渲染为 Markdown 文本。
func (r *Report) RenderMarkdown() string {
	var b strings.Builder
	fmt.Fprintf(&b, "# ProofCycle 审查报告 — %s\n\n", r.Job.Name)
	fmt.Fprintf(&b, "- 作业 ID：`%s`\n", r.Job.ID)
	fmt.Fprintf(&b, "- 作业状态：`%s`（当前版本 v%d）\n", r.Job.Status, r.Job.CurrentVer)
	fmt.Fprintf(&b, "- 设计师：`%s`　项目经理：`%s`\n\n", r.Job.DesignerID, r.Job.PMID)

	fmt.Fprintf(&b, "## 文件摘要（v%d）\n\n", r.File.VersionNo)
	fmt.Fprintf(&b, "| 项 | 值 |\n|---|---|\n")
	fmt.Fprintf(&b, "| 原始文件名 | %s |\n", escapeMD(r.File.FileName))
	fmt.Fprintf(&b, "| 类型 | %s |\n", r.File.ContentType)
	fmt.Fprintf(&b, "| 大小 | %d 字节 |\n", r.File.SizeBytes)
	fmt.Fprintf(&b, "| SHA-256 | `%s` |\n", r.File.SHA256)
	fmt.Fprintf(&b, "| 上传者 | `%s` |\n", r.File.UploadedBy)
	fmt.Fprintf(&b, "| 上传时间 | %s |\n\n", r.File.UploadedAt.Format(time.RFC3339))

	fmt.Fprintf(&b, "## 检查清单：%s（模板版本 %d，共 %d 项）\n\n",
		r.Checklist.Name, r.Checklist.SourceVersion, r.Checklist.TotalItems)
	fmt.Fprintf(&b, "审查总体状态：`%s`\n\n", r.ReviewStatus)
	fmt.Fprintf(&b, "| # | 编号 | 检查项 | 审查结论 |\n|---|---|---|---|\n")
	for _, it := range r.Items {
		fmt.Fprintf(&b, "| %d | %s | %s | %s |\n", it.OrderNo, it.Code, escapeMD(it.Text), formatFindings(it.Findings))
	}
	b.WriteString("\n")
	if r.Approval != nil {
		fmt.Fprintf(&b, "## 签核依据\n\n- 签核人：`%s`\n- 签核时间：%s\n\n```\n%s\n```\n",
			r.Approval.ApproverID, r.Approval.ApprovedAt.Format(time.RFC3339), r.Approval.Basis)
	} else {
		b.WriteString("## 签核依据\n\n尚未签核。\n")
	}
	fmt.Fprintf(&b, "\n> 报告生成时间：%s\n", r.GeneratedAt.Format(time.RFC3339))
	return b.String()
}

func reviewIDs(rs []domain.Review) []string {
	ids := make([]string, 0, len(rs))
	for _, r := range rs {
		ids = append(ids, r.ID)
	}
	if len(ids) == 0 {
		return []string{"__none__"}
	}
	return ids
}

func formatFindings(fs []ReportFinding) string {
	if len(fs) == 0 {
		return "（无意见）"
	}
	parts := make([]string, 0, len(fs))
	for _, f := range fs {
		s := fmt.Sprintf("%s:%s", f.ReviewerID, f.Result)
		if f.Result == domain.ResultFail {
			s += "（" + escapeMD(f.FailReason) + "）"
		}
		parts = append(parts, s)
	}
	return strings.Join(parts, "<br>")
}

func escapeMD(s string) string {
	s = strings.ReplaceAll(s, "|", "\\|")
	s = strings.ReplaceAll(s, "\n", " ")
	return s
}

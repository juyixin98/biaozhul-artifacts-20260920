package seed

import (
	"bytes"
	"context"
	"log"
	"time"

	"gorm.io/gorm"

	"proofcycle/internal/model"
	"proofcycle/internal/service"
)

// demoUser defines a deterministic demo principal.
type demoUser struct {
	name  string
	role  string
	token string
}

var demoUsers = []demoUser{
	{"demo-designer", model.RoleDesigner, "demo-designer-token"},
	{"demo-pm", model.RolePM, "demo-pm-token"},
	{"demo-reviewer-1", model.RoleReviewer, "demo-reviewer-1-token"},
	{"demo-reviewer-2", model.RoleReviewer, "demo-reviewer-2-token"},
}

// Seed inserts demo users, one job with a packaging checklist and one initial
// PDF revision. It is idempotent: a no-op when demo data already exists.
func Seed(ctx context.Context, db *gorm.DB, svc *service.Service) error {
	var count int64
	if err := db.Model(&model.User{}).Where("name = ?", demoUsers[0].name).Count(&count).Error; err != nil {
		return err
	}
	if count > 0 {
		log.Println("seed: demo data already present, skipping")
		return nil
	}

	now := time.Now().UTC()
	ids := map[string]int64{}
	for _, du := range demoUsers {
		u := &model.User{Name: du.name, Role: du.role, APIToken: du.token, CreatedAt: now}
		if err := db.Create(u).Error; err != nil {
			return err
		}
		ids[du.name] = u.ID
	}

	designer := &model.User{ID: ids["demo-designer"], Role: model.RoleDesigner}
	job, err := svc.CreateJob(ctx, designer, service.CreateJobInput{
		Name: "Folding Carton FC-2026-A",
		PMID: ids["demo-pm"],
		ReviewerIDs: []int64{
			ids["demo-reviewer-1"],
			ids["demo-reviewer-2"],
		},
		Checklist: []service.ChecklistInput{
			{Code: "COLOR-01", Description: "Printed colors match approved Pantone targets (Delta E <= 3)"},
			{Code: "DIM-02", Description: "Flat dimensions match die-line drawing within +/- 1 mm"},
			{Code: "BLEED-03", Description: "Artwork provides 3 mm bleed on every trimmed edge"},
			{Code: "BAR-04", Description: "EAN-13 barcode grade verified >= B at final size"},
			{Code: "TXT-05", Description: "Regulatory text present, legible and at minimum 6 pt"},
			{Code: "FIN-06", Description: "Specified substrate and surface finish match purchase order"},
		},
	})
	if err != nil {
		return err
	}

	// Minimal valid-enough PDF: real PDFs are supplied by the client; the
	// service validates the %PDF- file header, size and SHA-256, nothing more.
	pdf := bytes.NewReader([]byte("%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"))
	fv, _, err := svc.UploadRevision(ctx, designer, job.ID, pdf)
	if err != nil {
		return err
	}
	if err := svc.SetOriginalName(ctx, designer, fv.ID, "fc-2026-a_v1_dieline.pdf"); err != nil {
		return err
	}

	log.Println("seed: demo job created:", job.ID)
	log.Println("seed tokens: designer=demo-designer-token pm=demo-pm-token " +
		"reviewers=demo-reviewer-1-token,demo-reviewer-2-token")
	return nil
}

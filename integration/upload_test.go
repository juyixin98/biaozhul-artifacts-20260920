package integration

import (
	"bytes"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"testing"

	"proofcycle/internal/domain"
	"proofcycle/internal/seed"
	"proofcycle/internal/service"
)

// upload 上传一个首版/修订。
func (e *testEnv) upload(jobID, uploader, name string, body []byte, sha string) (*service.VersionDetail, error) {
	return e.svc.Version.UploadRevision(service.UploadInput{
		JobID:      jobID,
		UploaderID: uploader,
		FileName:   name,
		Reader:     bytes.NewReader(body),
		ExpectSHA:  sha,
		MaxSize:    1 << 20,
	})
}

// countFormalFiles 统计存储根目录下、除 tmp 暂存区外的常规文件数。
func countFormalFiles(root string) (int, error) {
	n := 0
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		rel, rErr := filepath.Rel(root, p)
		if rErr != nil {
			return rErr
		}
		if rel == "tmp" || filepath.Dir(rel) == "tmp" {
			return nil
		}
		n++
		return nil
	})
	return n, err
}

func TestUploadValidPDFAndPNGAndPersists(t *testing.T) {
	e := newTestEnv(t)
	job := e.mustCreateJob([]string{seed.Reviewer1})

	detail, err := e.upload(job.ID, seed.UserDesigner, "proof.pdf", validPDF(), "")
	if err != nil {
		t.Fatalf("upload pdf: %v", err)
	}
	if detail.VersionNo != 1 {
		t.Fatalf("first version = %d, want 1", detail.VersionNo)
	}
	if len(detail.Items) != 8 {
		t.Fatalf("snapshot items = %d, want 8", len(detail.Items))
	}
	if detail.Version.ContentType != "application/pdf" {
		t.Fatalf("content type = %s", detail.Version.ContentType)
	}

	d2, err := e.upload(job.ID, seed.UserDesigner, "proof.png", validPNG(), "")
	if err != nil {
		t.Fatalf("upload png revision: %v", err)
	}
	if d2.VersionNo != 2 || d2.Version.ContentType != "image/png" {
		t.Fatalf("v2 metadata wrong: no=%d type=%s", d2.VersionNo, d2.Version.ContentType)
	}
}

func TestUploadRejectsBadHeaderAndSizeAndHash(t *testing.T) {
	e := newTestEnv(t)
	job := e.mustCreateJob([]string{seed.Reviewer1})

	if _, err := e.upload(job.ID, seed.UserDesigner, "evil.pdf", []byte("GIF89a fake"), ""); !errors.Is(err, service.ErrValidation) {
		t.Fatalf("bad header: want validation err, got %v", err)
	}
	big := append([]byte("%PDF-1.4 "), bytes.Repeat([]byte("x"), 1<<20)...)
	if _, err := e.upload(job.ID, seed.UserDesigner, "big.pdf", big, ""); !errors.Is(err, service.ErrValidation) {
		t.Fatalf("oversize: want validation err, got %v", err)
	}
	if _, err := e.upload(job.ID, seed.UserDesigner, "p.pdf", validPDF(), "00"); err == nil {
		t.Fatalf("malformed hash should be rejected")
	}
	if _, err := e.upload(job.ID, seed.UserDesigner, "p.pdf", validPDF(),
		"6161616161616161616161616161616161616161616161616161616161616161"); err == nil {
		t.Fatalf("mismatched hash should be rejected")
	}

	var n int64
	e.db.Model(&domain.FileVersion{}).Count(&n)
	if n != 0 {
		t.Fatalf("failed uploads must not create versions, got %d", n)
	}
	if n, _ := countFormalFiles(e.dir); n != 0 {
		t.Fatalf("failed uploads must leave no files, got %d", n)
	}
}

// TestUploadInterruptedSweepsTemp 模拟上传在落盘前中断：临时文件残留，
// 进程重启时 SweepTemp 清理（main.go 启动恢复点）。
func TestUploadInterruptedSweepsTemp(t *testing.T) {
	e := newTestEnv(t)
	abandoned := filepath.Join(e.dir, "tmp", "upload-abandoned")
	if err := os.MkdirAll(filepath.Dir(abandoned), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(abandoned, validPDF(), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := e.store.SweepTemp(); err != nil {
		t.Fatalf("sweep: %v", err)
	}
	if _, err := os.Stat(abandoned); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("abandoned temp file must be swept, stat err=%v", err)
	}
}

// TestUploadDBFailureCompensatesFile 模拟“文件已落盘、数据库写入失败”：
// 事务回滚，已提交文件必须被补偿删除，不留孤儿。
func TestUploadDBFailureCompensatesFile(t *testing.T) {
	e := newTestEnv(t)
	job := e.mustCreateJob([]string{seed.Reviewer1})

	if err := e.db.Migrator().DropTable("file_versions"); err != nil {
		t.Fatalf("drop table: %v", err)
	}
	if _, err := e.upload(job.ID, seed.UserDesigner, "v1.pdf", validPDF(), ""); err == nil {
		t.Fatalf("upload with broken DB must fail")
	}
	n, err := countFormalFiles(e.dir)
	if err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("committed file must be compensated on DB failure, %d orphan(s) left", n)
	}
}

func TestRevisionsNeverOverwriteAndOldReviewDoesNotCount(t *testing.T) {
	e := newTestEnv(t)
	job := e.mustCreateJob([]string{seed.Reviewer1})

	d1, err := e.upload(job.ID, seed.UserDesigner, "v1.pdf", validPDF(), "")
	if err != nil {
		t.Fatal(err)
	}
	ids1 := e.snapshotItemIDs(d1.Version.ID)
	if _, err := e.svc.Review.Submit(service.SubmitReviewInput{
		JobID: job.ID, Reviewer: seed.Reviewer1, Items: allPassInputs(ids1),
	}); err != nil {
		t.Fatalf("review v1: %v", err)
	}
	if _, err := e.svc.Approval.SignOff(job.ID, seed.UserPM, 1); err != nil {
		t.Fatalf("approve v1: %v", err)
	}

	d2, err := e.upload(job.ID, seed.UserDesigner, "v2.pdf", append(validPDF(), []byte(" changed")...), "")
	if err != nil {
		t.Fatal(err)
	}
	if d2.VersionNo != 2 {
		t.Fatalf("want v2")
	}
	f, err := e.store.Open(d1.Version.StoredPath)
	if err != nil {
		t.Fatalf("v1 file must be preserved: %v", err)
	}
	f.Close()
	if d1.Version.StoredPath == d2.Version.StoredPath {
		t.Fatalf("revisions must use distinct paths")
	}

	var oldN int64
	e.db.Model(&domain.Review{}).Where("version_id = ?", d1.Version.ID).Count(&oldN)
	if oldN != 1 {
		t.Fatalf("old version review must be retained, got %d", oldN)
	}
	if _, err := e.svc.Approval.SignOff(job.ID, seed.UserPM, 2); !errors.Is(err, service.ErrNotAllReviewers) {
		t.Fatalf("approve v2 without new reviews: want ErrNotAllReviewers, got %v", err)
	}
	var refreshed domain.Job
	e.db.First(&refreshed, "id = ?", job.ID)
	if refreshed.Status != domain.StatusInReview {
		t.Fatalf("job must return to in_review after new revision, got %s", refreshed.Status)
	}
}

func TestConcurrentRevisionUploadsSerialize(t *testing.T) {
	e := newTestEnv(t)
	job := e.mustCreateJob([]string{seed.Reviewer1})

	errs := make(chan error, 2)
	vers := make(chan int, 2)
	for i := 0; i < 2; i++ {
		body := append(validPDF(), byte('0'+i))
		go func() {
			d, err := e.upload(job.ID, seed.UserDesigner, "rev.pdf", body, "")
			if err != nil {
				errs <- err
				return
			}
			vers <- d.VersionNo
		}()
	}
	got := map[int]bool{}
	for i := 0; i < 2; i++ {
		select {
		case err := <-errs:
			t.Fatalf("concurrent upload failed: %v", err)
		case v := <-vers:
			got[v] = true
		}
	}
	if !got[1] || !got[2] {
		t.Fatalf("expected versions 1 and 2, got %v", got)
	}
}

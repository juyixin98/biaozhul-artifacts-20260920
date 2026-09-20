package service_test

import (
	"bytes"
	"errors"
	"fmt"
	"io/fs"
	"path/filepath"
	"sync"
	"testing"

	"github.com/glebarez/sqlite"
	"gorm.io/gorm"

	"proofcycle/internal/migrate"
	"proofcycle/internal/models"
	"proofcycle/internal/service"
	"proofcycle/internal/storage"
)

// ---------------------------------------------------------------------------
// 测试环境
// ---------------------------------------------------------------------------

type env struct {
	svc *service.Service
	st  *storage.Storage
	db  *gorm.DB
}

func newEnv(t *testing.T, maxBytes int64) *env {
	t.Helper()
	dsn := fmt.Sprintf("file:%s?mode=memory&cache=shared", t.Name())
	db, err := gorm.Open(sqlite.Open(dsn), &gorm.Config{})
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	sqlDB, err := db.DB()
	if err != nil {
		t.Fatalf("db handle: %v", err)
	}
	sqlDB.SetMaxOpenConns(1) // 内存库串行化，等价于 MySQL 行锁下的串行提交
	if err := migrate.Run(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	if err := migrate.SeedDemo(db); err != nil {
		t.Fatalf("seed: %v", err)
	}
	st, err := storage.New(t.TempDir(), maxBytes)
	if err != nil {
		t.Fatalf("storage: %v", err)
	}
	return &env{svc: service.New(db, st), st: st, db: db}
}

func (e *env) user(t *testing.T, name string) *models.User {
	t.Helper()
	var u models.User
	if err := e.db.Where("name = ?", name).First(&u).Error; err != nil {
		t.Fatalf("user %s: %v", name, err)
	}
	return &u
}

func pdfBytes(n int) []byte {
	b := []byte("%PDF-1.4\n")
	return append(b, bytes.Repeat([]byte("x"), n-len(b))...)
}

func pngBytes(n int) []byte {
	b := []byte{0x89, 'P', 'N', 'G', 0x0D, 0x0A, 0x1A, 0x0A}
	return append(b, bytes.Repeat([]byte("y"), n-len(b))...)
}

// newJob 创建 alice(pm) + bob(designer) + carol/dave(审查员) 的作业。
func (e *env) newJob(t *testing.T) *models.Job {
	t.Helper()
	alice := e.user(t, "alice")
	bob := e.user(t, "bob")
	carol := e.user(t, "carol")
	dave := e.user(t, "dave")
	detail, err := e.svc.CreateJob(alice, service.CreateJobInput{
		Title:       "carton proof",
		DesignerID:  bob.ID,
		ReviewerIDs: []uint64{carol.ID, dave.ID},
		Checklist:   []string{"color matches spec", "barcode readable"},
	})
	if err != nil {
		t.Fatalf("create job: %v", err)
	}
	return detail.Job
}

func (e *env) upload(t *testing.T, jobID uint64) *models.Revision {
	t.Helper()
	bob := e.user(t, "bob")
	rev, err := e.svc.SubmitRevision(bob, jobID, "proof.pdf", bytes.NewReader(pdfBytes(256)))
	if err != nil {
		t.Fatalf("submit revision: %v", err)
	}
	return rev
}

func (e *env) checklist(t *testing.T, revID uint64) []models.ChecklistItem {
	t.Helper()
	var items []models.ChecklistItem
	if err := e.db.Where("revision_id = ?", revID).Order("seq").Find(&items).Error; err != nil {
		t.Fatalf("checklist: %v", err)
	}
	return items
}

// passAll 所有检查项通过、所有审查员完成。
func (e *env) passAll(t *testing.T, revID uint64) {
	t.Helper()
	carol := e.user(t, "carol")
	for _, it := range e.checklist(t, revID) {
		if _, err := e.svc.UpdateChecklistItem(carol, it.ID, service.ChecklistUpdateInput{
			Status: "pass", ExpectedVersion: it.Version,
		}); err != nil {
			t.Fatalf("pass item: %v", err)
		}
	}
	for _, name := range []string{"carol", "dave"} {
		if err := e.svc.CompleteReview(e.user(t, name), revID); err != nil {
			t.Fatalf("complete %s: %v", name, err)
		}
	}
}

func countFiles(t *testing.T, root string) int {
	t.Helper()
	n := 0
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() {
			n++
		}
		return nil
	})
	if err != nil {
		t.Fatalf("walk storage: %v", err)
	}
	return n
}

// ---------------------------------------------------------------------------
// 上传：中断、类型、超限
// ---------------------------------------------------------------------------

type failReader struct {
	data      []byte
	pos       int
	failAfter int
}

func (f *failReader) Read(p []byte) (int, error) {
	if f.pos >= f.failAfter {
		return 0, errors.New("connection reset by peer")
	}
	n := copy(p, f.data[f.pos:])
	f.pos += n
	return n, nil
}

func TestUploadInterruptedCleansUp(t *testing.T) {
	e := newEnv(t, 1<<20)
	job := e.newJob(t)
	bob := e.user(t, "bob")

	r := &failReader{data: pdfBytes(4096), failAfter: 100}
	if _, err := e.svc.SubmitRevision(bob, job.ID, "proof.pdf", r); err == nil {
		t.Fatal("expected error from interrupted upload")
	}
	var n int64
	e.db.Model(&models.Revision{}).Where("job_id = ?", job.ID).Count(&n)
	if n != 0 {
		t.Fatalf("revision row leaked after failed upload: %d", n)
	}
	if got := countFiles(t, e.st.Root); got != 0 {
		t.Fatalf("orphan files left after interrupted upload: %d", got)
	}
	var j models.Job
	e.db.First(&j, job.ID)
	if j.CurrentRevisionID != 0 {
		t.Fatalf("job current revision moved despite failed upload: %d", j.CurrentRevisionID)
	}
}

func TestUploadRejectsUnsupportedType(t *testing.T) {
	e := newEnv(t, 1<<20)
	job := e.newJob(t)
	bob := e.user(t, "bob")

	_, err := e.svc.SubmitRevision(bob, job.ID, "evil.exe", bytes.NewReader([]byte("MZ....not a proof")))
	if !errors.Is(err, storage.ErrUnsupportedType) {
		t.Fatalf("expected ErrUnsupportedType, got %v", err)
	}
	if got := countFiles(t, e.st.Root); got != 0 {
		t.Fatalf("files left after rejected upload: %d", got)
	}
}

func TestUploadRejectsOversize(t *testing.T) {
	e := newEnv(t, 64) // 64 字节上限
	job := e.newJob(t)
	bob := e.user(t, "bob")

	_, err := e.svc.SubmitRevision(bob, job.ID, "big.pdf", bytes.NewReader(pdfBytes(4096)))
	if !errors.Is(err, storage.ErrTooLarge) {
		t.Fatalf("expected ErrTooLarge, got %v", err)
	}
	if got := countFiles(t, e.st.Root); got != 0 {
		t.Fatalf("files left after oversize upload: %d", got)
	}
}

func TestUploadAcceptsPNG(t *testing.T) {
	e := newEnv(t, 1<<20)
	job := e.newJob(t)
	bob := e.user(t, "bob")
	rev, err := e.svc.SubmitRevision(bob, job.ID, "proof.png", bytes.NewReader(pngBytes(128)))
	if err != nil {
		t.Fatalf("png upload: %v", err)
	}
	if rev.Mime != "image/png" || rev.Size != 128 || len(rev.SHA256) != 64 {
		t.Fatalf("bad revision metadata: %+v", rev)
	}
}

// ---------------------------------------------------------------------------
// 路径越界与文件名清洗
// ---------------------------------------------------------------------------

func TestPathTraversalRejected(t *testing.T) {
	e := newEnv(t, 1<<20)
	for _, p := range []string{"../escape.pdf", "..", "a/../../b.pdf", "/etc/passwd", ""} {
		if _, err := e.st.AbsPath(p); !errors.Is(err, storage.ErrInvalidPath) {
			t.Fatalf("AbsPath(%q) should be rejected, got %v", p, err)
		}
	}
	abs, err := e.st.AbsPath("1/2-abc.pdf")
	if err != nil {
		t.Fatalf("legit path rejected: %v", err)
	}
	if len(abs) <= len(e.st.Root) || abs[:len(e.st.Root)] != e.st.Root {
		t.Fatalf("resolved path escapes root: %s", abs)
	}
	if _, err := e.st.Open("../../etc/passwd"); !errors.Is(err, storage.ErrInvalidPath) {
		t.Fatalf("Open traversal should be rejected, got %v", err)
	}
	if got := storage.SanitizeName("..\\..\\etc\\passwd"); got != "passwd" {
		t.Fatalf("sanitize failed: %q", got)
	}
}

// ---------------------------------------------------------------------------
// 版本失效：旧版本意见保留但不可写、不可作为签核依据
// ---------------------------------------------------------------------------

func TestSupersededRevisionIsReadOnly(t *testing.T) {
	e := newEnv(t, 1<<20)
	job := e.newJob(t)
	carol := e.user(t, "carol")

	rev1 := e.upload(t, job.ID)
	cm, err := e.svc.AddComment(carol, rev1.ID, "v1 comment")
	if err != nil {
		t.Fatalf("comment on v1: %v", err)
	}
	rev2 := e.upload(t, job.ID)

	// 旧版本拒绝一切写操作
	if _, err := e.svc.AddComment(carol, rev1.ID, "late comment"); !errors.Is(err, service.ErrInvalidState) {
		t.Fatalf("comment on superseded revision should fail, got %v", err)
	}
	if _, err := e.svc.UpdateComment(carol, cm.ID, "edit", cm.Version); !errors.Is(err, service.ErrInvalidState) {
		t.Fatalf("edit comment on superseded revision should fail, got %v", err)
	}
	items1 := e.checklist(t, rev1.ID)
	if _, err := e.svc.UpdateChecklistItem(carol, items1[0].ID, service.ChecklistUpdateInput{
		Status: "pass", ExpectedVersion: items1[0].Version,
	}); !errors.Is(err, service.ErrInvalidState) {
		t.Fatalf("checklist update on superseded revision should fail, got %v", err)
	}
	if err := e.svc.CompleteReview(carol, rev1.ID); !errors.Is(err, service.ErrInvalidState) {
		t.Fatalf("complete on superseded revision should fail, got %v", err)
	}

	// 旧版本意见保留可查
	hist, err := e.svc.GetRevisionHistory(carol, job.ID, rev1.ID)
	if err != nil {
		t.Fatalf("history of v1: %v", err)
	}
	if len(hist.Comments) != 1 || hist.Comments[0].Content != "v1 comment" {
		t.Fatalf("v1 comments not preserved: %+v", hist.Comments)
	}
	if hist.Revision.Status != models.RevisionSuperseded {
		t.Fatalf("v1 should be superseded, got %s", hist.Revision.Status)
	}

	// 新版本有独立的待处理清单与审查状态，不能沿用 v1 的完成记录
	items2 := e.checklist(t, rev2.ID)
	if len(items2) != len(items1) {
		t.Fatalf("v2 checklist snapshot missing")
	}
	for _, it := range items2 {
		if it.Status != models.ChecklistPending {
			t.Fatalf("v2 checklist should start pending, got %s", it.Status)
		}
	}
	var pending int64
	e.db.Model(&models.ReviewerState{}).Where("revision_id = ? AND state = ?", rev2.ID, models.ReviewPending).Count(&pending)
	if pending != 2 {
		t.Fatalf("v2 reviewer states should reset to pending, got %d", pending)
	}
}

// ---------------------------------------------------------------------------
// 失败项 / 未处理项拦截签核
// ---------------------------------------------------------------------------

func TestFailedOrPendingItemsBlockApproval(t *testing.T) {
	e := newEnv(t, 1<<20)
	job := e.newJob(t)
	alice := e.user(t, "alice")
	carol := e.user(t, "carol")
	rev := e.upload(t, job.ID)

	// 未处理项拦截
	if _, err := e.svc.Approve(alice, job.ID); !errors.Is(err, service.ErrInvalidState) {
		t.Fatalf("approve with pending items should fail, got %v", err)
	}

	// 失败必须写原因
	items := e.checklist(t, rev.ID)
	if _, err := e.svc.UpdateChecklistItem(carol, items[0].ID, service.ChecklistUpdateInput{
		Status: "fail", ExpectedVersion: items[0].Version,
	}); !errors.Is(err, service.ErrBadRequest) {
		t.Fatalf("fail without reason should be rejected, got %v", err)
	}

	// 失败项拦截
	if _, err := e.svc.UpdateChecklistItem(carol, items[0].ID, service.ChecklistUpdateInput{
		Status: "fail", FailReason: "color off", ExpectedVersion: items[0].Version,
	}); err != nil {
		t.Fatalf("fail with reason: %v", err)
	}
	if _, err := e.svc.UpdateChecklistItem(carol, items[1].ID, service.ChecklistUpdateInput{
		Status: "pass", ExpectedVersion: items[1].Version,
	}); err != nil {
		t.Fatalf("pass item 2: %v", err)
	}
	for _, name := range []string{"carol", "dave"} {
		if err := e.svc.CompleteReview(e.user(t, name), rev.ID); err != nil {
			t.Fatalf("complete: %v", err)
		}
	}
	if _, err := e.svc.Approve(alice, job.ID); !errors.Is(err, service.ErrInvalidState) {
		t.Fatalf("approve with failed item should fail, got %v", err)
	}

	// 改为通过后放行
	items = e.checklist(t, rev.ID)
	if _, err := e.svc.UpdateChecklistItem(carol, items[0].ID, service.ChecklistUpdateInput{
		Status: "pass", ExpectedVersion: items[0].Version,
	}); err != nil {
		t.Fatalf("re-pass item: %v", err)
	}
	ap, err := e.svc.Approve(alice, job.ID)
	if err != nil {
		t.Fatalf("approve should succeed: %v", err)
	}
	if ap.SHA256 != rev.SHA256 {
		t.Fatalf("approval digest mismatch")
	}
	// 重复签核被拒
	if _, err := e.svc.Approve(alice, job.ID); !errors.Is(err, service.ErrConflict) {
		t.Fatalf("double approve should conflict, got %v", err)
	}
	// 签核后作业冻结：新修订被拒
	bob := e.user(t, "bob")
	if _, err := e.svc.SubmitRevision(bob, job.ID, "v2.pdf", bytes.NewReader(pdfBytes(64))); !errors.Is(err, service.ErrInvalidState) {
		t.Fatalf("revision after approval should fail, got %v", err)
	}
}

func TestApprovalRequiresAllReviewersCompleted(t *testing.T) {
	e := newEnv(t, 1<<20)
	job := e.newJob(t)
	alice := e.user(t, "alice")
	carol := e.user(t, "carol")
	rev := e.upload(t, job.ID)

	for _, it := range e.checklist(t, rev.ID) {
		if _, err := e.svc.UpdateChecklistItem(carol, it.ID, service.ChecklistUpdateInput{
			Status: "na", ExpectedVersion: it.Version,
		}); err != nil {
			t.Fatalf("na item: %v", err)
		}
	}
	if err := e.svc.CompleteReview(carol, rev.ID); err != nil {
		t.Fatalf("complete carol: %v", err)
	}
	// dave 未完成
	if _, err := e.svc.Approve(alice, job.ID); !errors.Is(err, service.ErrInvalidState) {
		t.Fatalf("approve without all reviewers should fail, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// 并发签核：仅一个成功
// ---------------------------------------------------------------------------

func TestConcurrentApprovalOnlyOneSucceeds(t *testing.T) {
	e := newEnv(t, 1<<20)
	job := e.newJob(t)
	alice := e.user(t, "alice")
	rev := e.upload(t, job.ID)
	e.passAll(t, rev.ID)

	const n = 8
	var wg sync.WaitGroup
	results := make(chan error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := e.svc.Approve(alice, job.ID)
			results <- err
		}()
	}
	wg.Wait()
	close(results)

	succeeded := 0
	for err := range results {
		if err == nil {
			succeeded++
		} else if !errors.Is(err, service.ErrConflict) && !errors.Is(err, service.ErrInvalidState) {
			t.Fatalf("unexpected error: %v", err)
		}
	}
	if succeeded != 1 {
		t.Fatalf("expected exactly 1 successful approval, got %d", succeeded)
	}
	var approvals int64
	e.db.Model(&models.Approval{}).Where("job_id = ?", job.ID).Count(&approvals)
	if approvals != 1 {
		t.Fatalf("expected 1 approval record, got %d", approvals)
	}
}

// ---------------------------------------------------------------------------
// 乐观锁：意见与检查项版本冲突
// ---------------------------------------------------------------------------

func TestOptimisticLockOnCommentAndChecklist(t *testing.T) {
	e := newEnv(t, 1<<20)
	job := e.newJob(t)
	carol := e.user(t, "carol")
	rev := e.upload(t, job.ID)

	cm, err := e.svc.AddComment(carol, rev.ID, "first")
	if err != nil {
		t.Fatalf("comment: %v", err)
	}
	if _, err := e.svc.UpdateComment(carol, cm.ID, "second", cm.Version); err != nil {
		t.Fatalf("first update: %v", err)
	}
	if _, err := e.svc.UpdateComment(carol, cm.ID, "stale", cm.Version); !errors.Is(err, service.ErrConflict) {
		t.Fatalf("stale comment update should conflict, got %v", err)
	}

	it := e.checklist(t, rev.ID)[0]
	if _, err := e.svc.UpdateChecklistItem(carol, it.ID, service.ChecklistUpdateInput{
		Status: "pass", ExpectedVersion: it.Version,
	}); err != nil {
		t.Fatalf("checklist update: %v", err)
	}
	if _, err := e.svc.UpdateChecklistItem(carol, it.ID, service.ChecklistUpdateInput{
		Status: "fail", FailReason: "x", ExpectedVersion: it.Version,
	}); !errors.Is(err, service.ErrConflict) {
		t.Fatalf("stale checklist update should conflict, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// 角色隔离
// ---------------------------------------------------------------------------

func TestRoleIsolation(t *testing.T) {
	e := newEnv(t, 1<<20)
	job := e.newJob(t)
	alice := e.user(t, "alice")
	bob := e.user(t, "bob")
	carol := e.user(t, "carol")
	dave := e.user(t, "dave")
	erin := e.user(t, "erin") // 未加入作业
	rev := e.upload(t, job.ID)

	// 非参与者不可见
	if _, err := e.svc.GetJob(erin, job.ID); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("outsider GetJob should be forbidden, got %v", err)
	}
	if _, err := e.svc.GetReport(erin, job.ID, 0); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("outsider GetReport should be forbidden, got %v", err)
	}
	if _, err := e.svc.GetRevisionHistory(erin, job.ID, rev.ID); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("outsider history should be forbidden, got %v", err)
	}
	if _, _, err := e.svc.OpenRevisionFile(erin, job.ID, rev.ID); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("outsider download should be forbidden, got %v", err)
	}

	// 审查员不能签核
	if _, err := e.svc.Approve(carol, job.ID); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("reviewer approve should be forbidden, got %v", err)
	}
	// 设计师不能批准自己的作业
	if _, err := e.svc.Approve(bob, job.ID); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("designer approve should be forbidden, got %v", err)
	}
	// 非设计师不能提交修订
	if _, err := e.svc.SubmitRevision(carol, job.ID, "x.pdf", bytes.NewReader(pdfBytes(32))); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("reviewer submit revision should be forbidden, got %v", err)
	}
	// 审查员只能改自己的意见
	cm, err := e.svc.AddComment(carol, rev.ID, "mine")
	if err != nil {
		t.Fatalf("comment: %v", err)
	}
	if _, err := e.svc.UpdateComment(dave, cm.ID, "hijack", cm.Version); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("updating another reviewer's comment should be forbidden, got %v", err)
	}
	// 设计师不能发表审查意见
	if _, err := e.svc.AddComment(bob, rev.ID, "designer comment"); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("designer comment should be forbidden, got %v", err)
	}
	// 项目经理兼设计师时也不能批准自己的作业
	selfJob, err := e.svc.CreateJob(alice, service.CreateJobInput{
		Title: "self design", DesignerID: alice.ID, Checklist: []string{"x"},
	})
	if err != nil {
		t.Fatalf("create self-design job: %v", err)
	}
	if _, err := e.svc.Approve(alice, selfJob.ID); !errors.Is(err, service.ErrForbidden) {
		t.Fatalf("designer-pm self approval should be forbidden, got %v", err)
	}
}

func TestMaxEightReviewers(t *testing.T) {
	e := newEnv(t, 1<<20)
	alice := e.user(t, "alice")
	bob := e.user(t, "bob")
	ids := make([]uint64, 0, 9)
	for _, name := range []string{"carol", "dave", "erin", "frank", "grace", "heidi", "ivan", "judy"} {
		ids = append(ids, e.user(t, name).ID)
	}
	if _, err := e.svc.CreateJob(alice, service.CreateJobInput{
		Title: "ok", DesignerID: bob.ID, ReviewerIDs: ids, Checklist: []string{"x"},
	}); err != nil {
		t.Fatalf("8 reviewers should be allowed: %v", err)
	}
	ids = append(ids, alice.ID)
	if _, err := e.svc.CreateJob(alice, service.CreateJobInput{
		Title: "too many", DesignerID: bob.ID, ReviewerIDs: ids, Checklist: []string{"x"},
	}); !errors.Is(err, service.ErrBadRequest) {
		t.Fatalf("9 reviewers should be rejected, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// 报告导出
// ---------------------------------------------------------------------------

func TestReportContents(t *testing.T) {
	e := newEnv(t, 1<<20)
	job := e.newJob(t)
	alice := e.user(t, "alice")
	carol := e.user(t, "carol")
	rev := e.upload(t, job.ID)
	if _, err := e.svc.AddComment(carol, rev.ID, "looks good"); err != nil {
		t.Fatalf("comment: %v", err)
	}
	e.passAll(t, rev.ID)
	if _, err := e.svc.Approve(alice, job.ID); err != nil {
		t.Fatalf("approve: %v", err)
	}

	rep, err := e.svc.GetReport(carol, job.ID, 0)
	if err != nil {
		t.Fatalf("report: %v", err)
	}
	if rep.Revision.SHA256 != rev.SHA256 || rep.Revision.Size != rev.Size {
		t.Fatalf("report missing file digest: %+v", rep.Revision)
	}
	if len(rep.Checklist) != 2 {
		t.Fatalf("report missing checklist snapshot")
	}
	if rep.Approval == nil || rep.Approval.SHA256 != rev.SHA256 {
		t.Fatalf("report missing sign-off basis")
	}
	if len(rep.ReviewerStates) != 2 {
		t.Fatalf("report missing reviewer states")
	}
	for _, st := range rep.ReviewerStates {
		if st.State != models.ReviewCompleted {
			t.Fatalf("reviewer state not completed: %+v", st)
		}
	}
	if len(rep.Comments) != 1 {
		t.Fatalf("report missing comments")
	}
}

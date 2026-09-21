package service

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"strings"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"

	"proofcycle/internal/domain"
	"proofcycle/internal/storage"
	"proofcycle/internal/util"
)

// VersionService 管理文件版本（首版与后续修订）、下载与按版本历史。
type VersionService struct {
	db    *gorm.DB
	store Store
}

// StagedUpload 是暂存完成、等待落盘的上传（服务层封装）。
type StagedUpload struct {
	file *storage.StagedFile
}

// Kind 检测到的文件种类。
func (u *StagedUpload) Kind() storage.Kind { return u.file.Kind() }

// Size 文件字节数。
func (u *StagedUpload) Size() int64 { return u.file.Size() }

// SHA256 实际内容摘要。
func (u *StagedUpload) SHA256() string { return u.file.SHA256() }

// Commit 原子移动到服务生成的相对路径。
func (u *StagedUpload) Commit(relPath string) error { return u.file.Commit(relPath) }

// Release 放弃暂存文件。
func (u *StagedUpload) Release() { u.file.Release() }

// Store 是上传落盘所需的存储能力（*storage.LocalStore 天然满足）。
type Store interface {
	Stage(src io.Reader, maxSize int64, expectSHA string) (*StagedUpload, error)
	Delete(relPath string) error
	Open(relPath string) (*os.File, error)
}

// localStoreAdapter 让 *storage.LocalStore 返回服务层的 StagedUpload。
type localStoreAdapter struct{ inner *storage.LocalStore }

// WrapStore 适配具体的本地存储器。
func WrapStore(ls *storage.LocalStore) Store { return &localStoreAdapter{inner: ls} }

func (a *localStoreAdapter) Stage(src io.Reader, maxSize int64, expectSHA string) (*StagedUpload, error) {
	f, err := a.inner.Stage(src, maxSize, expectSHA)
	if err != nil {
		return nil, err
	}
	return &StagedUpload{file: f}, nil
}

func (a *localStoreAdapter) Delete(relPath string) error           { return a.inner.Delete(relPath) }
func (a *localStoreAdapter) Open(relPath string) (*os.File, error) { return a.inner.Open(relPath) }

// UploadInput 上传/修订参数。
type UploadInput struct {
	JobID      string
	UploaderID string
	FileName   string
	Reader     io.Reader
	ExpectSHA  string // 可选：客户端声明的 SHA-256（十六进制），非空则强校验
	MaxSize    int64
}

// VersionDetail 返回给调用方的新版本信息。
type VersionDetail struct {
	Version   *domain.FileVersion
	Snapshot  *domain.ChecklistSnapshot
	Items     []domain.ChecklistSnapshotItem
	VersionNo int
}

// UploadRevision 上传首个文件或后续修订。
//
// 落盘与数据库的一致性策略：
//  1. 内容先写临时文件并完成大小/文件头/SHA-256 校验（此时不动正式目录）；
//  2. 事务内锁作业行，生成版本号与服务端存储路径，先 rename 落盘再写库；
//  3. 写库任一步失败：回滚事务并删除已落盘文件，释放临时文件；
//  4. 成功：修订文件以新路径新增，永不覆盖旧版本；旧版本打上 SupersededByID。
//
// 行锁保证“并发上传修订 / 签核 / 修改意见”在同一作业上串行，检查不会被绕过。
func (s *VersionService) UploadRevision(in UploadInput) (*VersionDetail, error) {
	if in.MaxSize <= 0 {
		return nil, errors.Join(ErrValidation, errors.New("missing max upload size"))
	}
	origName := sanitizeName(in.FileName)
	if origName == "" {
		return nil, errors.Join(ErrValidation, errors.New("file name is required"))
	}

	// 第一步：暂存 + 内容校验（耗时的 IO 在事务外完成）。
	sf, err := s.store.Stage(in.Reader, in.MaxSize, in.ExpectSHA)
	if err != nil {
		return nil, mapStorageError(err)
	}

	// 第二、三步：锁作业、定版本号、落盘、写库。
	var detail *VersionDetail
	var storedRel string
	committed := false

	txErr := s.db.Transaction(func(tx *gorm.DB) error {
		job, err := lockJob(tx, in.JobID)
		if err != nil {
			return err
		}
		if job.DesignerID != in.UploaderID {
			return ErrNotDesigner
		}

		// 复制当前清单为快照。
		var cl domain.Checklist
		if err := tx.First(&cl, "id = ?", job.ChecklistID).Error; err != nil {
			return ErrUnknownChecklist
		}
		var tmplItems []domain.ChecklistItem
		if err := tx.Where("checklist_id = ?", cl.ID).Order("order_no").Find(&tmplItems).Error; err != nil {
			return err
		}
		if len(tmplItems) == 0 {
			return errors.Join(ErrValidation, errors.New("checklist has no items"))
		}

		newNo := job.CurrentVersionNo + 1
		snapshot := &domain.ChecklistSnapshot{
			ID:            util.NewID(),
			JobID:         job.ID,
			ChecklistID:   cl.ID,
			ChecklistName: cl.Name,
			SourceVersion: cl.Version,
			CreatedAt:     time.Now().UTC(),
		}
		if err := tx.Create(snapshot).Error; err != nil {
			return err
		}
		snapItems := make([]domain.ChecklistSnapshotItem, 0, len(tmplItems))
		for _, it := range tmplItems {
			snapItems = append(snapItems, domain.ChecklistSnapshotItem{
				ID:         util.NewID(),
				SnapshotID: snapshot.ID,
				OrderNo:    it.OrderNo,
				Code:       it.Code,
				Text:       it.Text,
			})
		}
		if err := tx.Create(&snapItems).Error; err != nil {
			return err
		}

		// 服务端生成存储路径；原始文件名仅入库展示，绝不参与路径拼接。
		rel := path.Join("jobs", job.ID, fmt.Sprintf("v%d-%s.%s", newNo, util.NewID()[:12], sf.Kind()))

		version := &domain.FileVersion{
			ID:           util.NewID(),
			JobID:        job.ID,
			VersionNo:    newNo,
			UploadedByID: in.UploaderID,
			FileName:     origName,
			StoredPath:   rel,
			ContentType:  sf.Kind().ContentType(),
			SizeBytes:    sf.Size(),
			SHA256:       sf.SHA256(),
			SnapshotID:   snapshot.ID,
			CreatedAt:    time.Now().UTC(),
		}

		// 先落盘（同文件系统原子 rename），成功后再写库。
		if err := sf.Commit(rel); err != nil {
			return mapStorageError(err)
		}
		storedRel = rel
		committed = true // 此后任一步 DB 失败，事务回滚后都要补偿删除该文件。

		if err := tx.Create(version).Error; err != nil {
			return err
		}
		// 回填快照的 VersionID。
		if err := tx.Model(&domain.ChecklistSnapshot{}).Where("id = ?", snapshot.ID).
			Update("version_id", version.ID).Error; err != nil {
			return err
		}
		// 上一版本标记被取代（历史与旧意见保留，但不能作为新版本批准依据）。
		if job.CurrentVersionNo > 0 {
			if err := tx.Model(&domain.FileVersion{}).
				Where("job_id = ? AND version_no = ?", job.ID, job.CurrentVersionNo).
				Update("superseded_by_id", version.ID).Error; err != nil {
				return err
			}
		}
		// 作业回到待审查：新修订必须重新走完审查与签核。
		if err := tx.Model(&domain.Job{}).Where("id = ?", job.ID).Updates(map[string]any{
			"current_version_no": newNo,
			"status":             domain.StatusInReview,
			"updated_at":         time.Now().UTC(),
		}).Error; err != nil {
			return err
		}

		snapshot.Items = snapItems
		detail = &VersionDetail{Version: version, Snapshot: snapshot, Items: snapItems, VersionNo: newNo}
		return nil
	})

	if txErr != nil {
		// 补偿：文件已落盘但数据库失败 -> 删除文件；临时文件释放。
		if committed && storedRel != "" {
			_ = s.store.Delete(storedRel)
		}
		sf.Release()
		return nil, txErr
	}
	// 事务成功后临时文件已被 rename，Release 为空操作；保留以覆盖边界情况。
	sf.Release()
	return detail, nil
}

// lockJob 在事务内以 FOR UPDATE 锁定作业行，是所有写操作的串行点。
func lockJob(tx *gorm.DB, jobID string) (*domain.Job, error) {
	var job domain.Job
	// MySQL InnoDB: FOR UPDATE 行锁；并发的上传/审查/签核在此排队。
	if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).First(&job, "id = ?", jobID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &job, nil
}

// CurrentVersion 返回作业当前文件版本（含快照）。无版本时返回 ErrVersionMissing。
func (s *VersionService) CurrentVersion(jobID string) (*domain.FileVersion, error) {
	var job domain.Job
	if err := s.db.First(&job, "id = ?", jobID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	if job.CurrentVersionNo == 0 {
		return nil, ErrVersionMissing
	}
	var v domain.FileVersion
	if err := s.db.Preload("Snapshot").
		First(&v, "job_id = ? AND version_no = ?", jobID, job.CurrentVersionNo).Error; err != nil {
		return nil, err
	}
	return &v, nil
}

// GetVersion 按版本号取文件版本（含快照），不存在返回 ErrNotFound。
func (s *VersionService) GetVersion(jobID string, versionNo int) (*domain.FileVersion, error) {
	var v domain.FileVersion
	err := s.db.Preload("Snapshot").
		First(&v, "job_id = ? AND version_no = ?", jobID, versionNo).Error
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &v, nil
}

// VersionHistory 某版本的完整审查历史。
type VersionHistory struct {
	Version  domain.FileVersion
	Items    []domain.ChecklistSnapshotItem
	Reviews  []ReviewDetail
	Approval *domain.Approval
}

// ReviewDetail 意见及其条目。
type ReviewDetail struct {
	Review domain.Review
	Items  []domain.ReviewItem
}

// History 返回某版本的完整审查历史。
func (s *VersionService) History(jobID string, versionNo int) (*VersionHistory, error) {
	v, err := s.GetVersion(jobID, versionNo)
	if err != nil {
		return nil, err
	}
	h := &VersionHistory{Version: *v}

	if err := s.db.Where("snapshot_id = ?", v.SnapshotID).
		Order("order_no").Find(&h.Items).Error; err != nil {
		return nil, err
	}
	var reviews []domain.Review
	if err := s.db.Where("version_id = ?", v.ID).Order("submitted_at").Find(&reviews).Error; err != nil {
		return nil, err
	}
	for _, r := range reviews {
		var items []domain.ReviewItem
		if err := s.db.Where("review_id = ?", r.ID).Find(&items).Error; err != nil {
			return nil, err
		}
		h.Reviews = append(h.Reviews, ReviewDetail{Review: r, Items: items})
	}
	var ap domain.Approval
	err = s.db.First(&ap, "version_id = ?", v.ID).Error
	if err == nil {
		h.Approval = &ap
	} else if !errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, err
	}
	return h, nil
}

// ListVersions 列出作业全部版本（按版本号升序）。
func (s *VersionService) ListVersions(jobID string) ([]domain.FileVersion, error) {
	var vs []domain.FileVersion
	if err := s.db.Where("job_id = ?", jobID).Order("version_no").Find(&vs).Error; err != nil {
		return nil, err
	}
	return vs, nil
}

// mapStorageError 把存储层错误映射为业务错误（HTTP 层可区分 4xx）。
func mapStorageError(err error) error {
	switch {
	case errors.Is(err, storage.ErrTooLarge):
		return errors.Join(ErrValidation, err)
	case errors.Is(err, storage.ErrEmptyFile):
		return errors.Join(ErrValidation, err)
	case errors.Is(err, storage.ErrUnsupportedKind), errors.Is(err, storage.ErrBadHeader):
		return errors.Join(ErrValidation, err)
	case errors.Is(err, storage.ErrHashMismatch):
		return errors.Join(ErrValidation, err)
	case errors.Is(err, storage.ErrPathEscape):
		return err
	default:
		return err
	}
}

// sanitizeName 仅保留一个安全的展示文件名（路径分隔符等一律剥除）。
func sanitizeName(name string) string {
	name = strings.TrimSpace(name)
	name = strings.ReplaceAll(name, "\x00", "")
	name = path.Base(name)
	name = strings.TrimPrefix(name, ".")
	if name == "/" || name == "." || name == ".." {
		return ""
	}
	return name
}

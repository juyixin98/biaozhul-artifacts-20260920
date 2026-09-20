// Package evidence 实现证据登记与完整性复核作业。
package evidence

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"time"

	"gorm.io/gorm"

	"forensiccore/internal/chain"
	"forensiccore/internal/hashutil"
	"forensiccore/internal/models"
	"forensiccore/internal/safeopen"
)

// DefaultChunkSize 默认分块大小 4 MiB。
const DefaultChunkSize = 4 << 20

// chunkOwnerJob ChunkHash.OwnerType 取值。
const chunkOwnerJob = "job"

// Service 证据服务。
type Service struct {
	DB       *gorm.DB
	Root     *safeopen.Root
	Chain    *chain.Store
	ChunkSize int64

	// AfterChunkHook 在每块处理完成后回调（参数为已处理字节数），
	// 用于进度汇报与测试注入（如模拟读取期间文件被修改、中断作业）。
	AfterChunkHook func(done int64)
}

// NewService 创建证据服务。
func NewService(db *gorm.DB, root *safeopen.Root, chainStore *chain.Store, chunkSize int64) *Service {
	if chunkSize <= 0 {
		chunkSize = DefaultChunkSize
	}
	return &Service{DB: db, Root: root, Chain: chainStore, ChunkSize: chunkSize}
}

// Register 登记白名单目录内的镜像：只读分块计算 SHA-256 并保存基线。
// 计算期间文件发生变化则失败，不保存任何基线。
func (s *Service) Register(caseID uint, relPath, actor string) (*models.Evidence, error) {
	var c models.Case
	if err := s.DB.First(&c, caseID).Error; err != nil {
		return nil, fmt.Errorf("case %d not found: %w", caseID, err)
	}
	f, _, err := s.Root.OpenReadOnly(relPath)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	digest, size, id, err := hashutil.HashWholeFile(f, s.ChunkSize, s.AfterChunkHook)
	if err != nil {
		// 读取期间文件变化：拒绝登记，不保存错误基线。
		return nil, err
	}

	ev := models.Evidence{
		CaseID:       caseID,
		RelPath:      relPath,
		Size:         size,
		SHA256:       digest,
		Dev:          id.Dev,
		Ino:          id.Ino,
		MtimeNsec:    id.MtimeNsec,
		CtimeNsec:    id.CtimeNsec,
		RegisteredBy: actor,
	}
	err = s.DB.Transaction(func(tx *gorm.DB) error {
		if err := tx.Create(&ev).Error; err != nil {
			return err
		}
		p, err := chain.CanonicalJSON(map[string]any{
			"evidence_id": ev.ID,
			"rel_path":    ev.RelPath,
			"size":        ev.Size,
			"sha256":      ev.SHA256,
		})
		if err != nil {
			return err
		}
		return s.Chain.AppendTx(tx, caseID, models.EventRegister, actor, p, nil)
	})
	if err != nil {
		return nil, err
	}
	return &ev, nil
}

// CreateVerifyJob 为已登记证据创建复核作业。
func (s *Service) CreateVerifyJob(evidenceID uint) (*models.VerifyJob, error) {
	var ev models.Evidence
	if err := s.DB.First(&ev, evidenceID).Error; err != nil {
		return nil, fmt.Errorf("evidence %d not found: %w", evidenceID, err)
	}
	job := models.VerifyJob{
		CaseID:     ev.CaseID,
		EvidenceID: ev.ID,
		Status:     models.JobPending,
		TotalSize:  ev.Size,
	}
	if err := s.DB.Create(&job).Error; err != nil {
		return nil, err
	}
	return &job, nil
}

// GetJob 读取作业当前状态。
func (s *Service) GetJob(jobID uint) (*models.VerifyJob, error) {
	var job models.VerifyJob
	if err := s.DB.First(&job, jobID).Error; err != nil {
		return nil, err
	}
	return &job, nil
}

// RecoverInterruptedJobs 进程重启后把仍处于 running 的作业标记为 paused，
// 等待显式恢复（恢复前会重新校验文件身份与已处理部分）。
func (s *Service) RecoverInterruptedJobs() error {
	return s.DB.Model(&models.VerifyJob{}).
		Where("status = ?", models.JobRunning).
		Update("status", models.JobPaused).Error
}

// RunJob 执行或恢复复核作业，同步运行直至完成、失败或 ctx 取消（暂停）。
// 恢复时先校验文件身份（设备号/inode/大小，而非文件名），再重算已处理
// 前缀并与持久化的分块摘要逐一比对，确认已处理部分未变化后才继续。
func (s *Service) RunJob(ctx context.Context, jobID uint, actor string) (*models.VerifyJob, error) {
	var job models.VerifyJob
	if err := s.DB.First(&job, jobID).Error; err != nil {
		return nil, fmt.Errorf("job %d not found: %w", jobID, err)
	}
	switch job.Status {
	case models.JobCompleted, models.JobFailed:
		return nil, fmt.Errorf("job %d already finished with status %s", job.ID, job.Status)
	case models.JobRunning:
		return nil, errors.New("job is already running")
	}
	var ev models.Evidence
	if err := s.DB.First(&ev, job.EvidenceID).Error; err != nil {
		return nil, err
	}

	f, id, err := s.Root.OpenReadOnly(ev.RelPath)
	if err != nil {
		s.failJob(&job, fmt.Sprintf("无法打开证据文件: %v", err))
		return &job, nil
	}
	defer f.Close()

	// 文件身份校验：设备号 + inode + 大小，而非文件名。
	if id.Dev != ev.Dev || id.Ino != ev.Ino {
		s.failJob(&job, "文件身份已改变（设备号/inode 与登记基线不符），拒绝继续")
		return &job, nil
	}
	if id.Size != ev.Size || id.Size != job.TotalSize {
		s.failJob(&job, "文件大小已改变，已记录的进度失效，拒绝继续")
		return &job, nil
	}
	// 续算（已有进度）时额外要求 mtime/ctime 与基线一致：
	// inode 号可能被复用，ctime 能识别"同名同内容但已替换"的文件。
	// 时间戳不符说明登记后文件被触碰过，部分进度不可信，必须重新起作业。
	if job.ProcessedBytes > 0 && (id.MtimeNsec != ev.MtimeNsec || id.CtimeNsec != ev.CtimeNsec) {
		s.failJob(&job, "文件在登记后被修改过（mtime/ctime 与基线不符），拒绝续算，请新建复核作业")
		return &job, nil
	}

	job.Status = models.JobRunning
	job.Error = ""
	if err := s.DB.Save(&job).Error; err != nil {
		return nil, err
	}

	h := sha256.New()
	processed := job.ProcessedBytes

	// 恢复场景：重算已处理前缀并与持久化的分块摘要比对。
	if processed > 0 {
		var chunks []models.ChunkHash
		if err := s.DB.Where("owner_type = ? AND owner_id = ?", chunkOwnerJob, job.ID).
			Order("seq ASC").Find(&chunks).Error; err != nil {
			return nil, err
		}
		var off int64
		for _, ch := range chunks {
			if ch.Offset != off {
				s.failJob(&job, fmt.Sprintf("进度记录不连续：块 %d 偏移 %d，期望 %d", ch.Seq, ch.Offset, off))
				return &job, nil
			}
			data, rerr := hashutil.ReadChunkAt(f, off, ch.Length)
			if rerr != nil {
				s.failJob(&job, fmt.Sprintf("重读已处理部分失败: %v", rerr))
				return &job, nil
			}
			if sum := hashutil.SHA256Hex(data); sum != ch.SHA256 {
				s.failJob(&job, fmt.Sprintf("已处理部分在中断期间被修改（块 %d 摘要不符），拒绝续算", ch.Seq))
				return &job, nil
			}
			h.Write(data)
			off += ch.Length
		}
		if off != processed {
			s.failJob(&job, fmt.Sprintf("进度 %d 与分块记录总量 %d 不一致", processed, off))
			return &job, nil
		}
	}

	// 主循环：分块读取、持久化每块摘要与进度。
	seq := 0
	s.DB.Where("owner_type = ? AND owner_id = ?", chunkOwnerJob, job.ID).
		Select("COALESCE(MAX(seq), -1) + 1").Scan(&seq)
	for processed < id.Size {
		if ctx.Err() != nil {
			job.Status = models.JobPaused
			if err := s.DB.Save(&job).Error; err != nil {
				return nil, err
			}
			return &job, nil
		}
		n := s.ChunkSize
		if rem := id.Size - processed; rem < n {
			n = rem
		}
		data, rerr := hashutil.ReadChunkAt(f, processed, n)
		if rerr != nil {
			s.failJob(&job, fmt.Sprintf("读取失败（文件可能在复核期间被修改）: %v", rerr))
			return &job, nil
		}
		sum := hashutil.SHA256Hex(data)
		h.Write(data)
		err := s.DB.Transaction(func(tx *gorm.DB) error {
			if err := tx.Create(&models.ChunkHash{
				OwnerType: chunkOwnerJob,
				OwnerID:   job.ID,
				Seq:       seq,
				Offset:    processed,
				Length:    n,
				SHA256:    sum,
			}).Error; err != nil {
				return err
			}
			return tx.Model(&models.VerifyJob{}).Where("id = ?", job.ID).
				Update("processed_bytes", processed+n).Error
		})
		if err != nil {
			return nil, err
		}
		processed += n
		seq++
		job.ProcessedBytes = processed // 与库内进度保持一致，暂停/失败落库时不会回退
		if s.AfterChunkHook != nil {
			s.AfterChunkHook(processed)
		}
	}

	// 复核期间发生变化：作废本次结果。
	if err := hashutil.CheckUnchanged(f, id); err != nil {
		s.failJob(&job, "复核过程中文件发生变化，结果作废")
		return &job, nil
	}

	final := fmt.Sprintf("%x", h.Sum(nil))
	result := models.ResultMatch
	if final != ev.SHA256 {
		result = models.ResultMismatch
	}
	now := time.Now()
	err = s.DB.Transaction(func(tx *gorm.DB) error {
		job.Status = models.JobCompleted
		job.Result = result
		job.ProcessedBytes = processed
		job.FinishedAt = &now
		if err := tx.Save(&job).Error; err != nil {
			return err
		}
		p, err := chain.CanonicalJSON(map[string]any{
			"job_id":      job.ID,
			"evidence_id": ev.ID,
			"result":      result,
			"sha256":      final,
			"bytes":       processed,
		})
		if err != nil {
			return err
		}
		return s.Chain.AppendTx(tx, job.CaseID, models.EventVerify, actor, p, nil)
	})
	if err != nil {
		return nil, err
	}
	return &job, nil
}

func (s *Service) failJob(job *models.VerifyJob, msg string) {
	now := time.Now()
	job.Status = models.JobFailed
	job.Error = msg
	job.FinishedAt = &now
	s.DB.Save(job)
}

package service

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"time"

	"golang.org/x/sys/unix"
	"gorm.io/gorm"

	"github.com/example/forensiccore/internal/chain"
	"github.com/example/forensiccore/internal/hashfile"
	"github.com/example/forensiccore/internal/model"
)

// StartVerify 创建一个复核作业并入队（调查员）。
// 同一证据同时只允许一个活动作业（queued/running/interrupted）。
func (s *Service) StartVerify(caseID, evidenceID uint, actor string) (*model.VerifyJob, error) {
	ev, err := s.GetEvidence(caseID, evidenceID)
	if err != nil {
		return nil, err
	}

	var active int64
	if err := s.db.Model(&model.VerifyJob{}).
		Where("evidence_id = ? AND status IN ?",
			ev.ID, []string{model.VerifyStatusQueued, model.VerifyStatusRunning, model.VerifyStatusInterrupted}).
		Count(&active).Error; err != nil {
		return nil, err
	}
	if active > 0 {
		return nil, kindErr(KindConflict, "an active verification job already exists for this evidence")
	}

	job := &model.VerifyJob{
		EvidenceID: ev.ID,
		CaseID:     caseID,
		Status:     model.VerifyStatusQueued,
		ChunkSize:  s.cfg.ChunkSize,
		StartedBy:  actor,
		StartedAt:  time.Now().UTC(),
	}
	if err := s.db.Create(job).Error; err != nil {
		return nil, err
	}
	s.enqueue(job.ID)
	return job, nil
}

// ResumeJob 显式恢复一个中断的复核作业（调查员）。恢复执行时会先验证
// 文件身份（dev/ino）与全部已处理块摘要，不满足则作业失败。
func (s *Service) ResumeJob(caseID, jobID uint) (*model.VerifyJob, error) {
	job, _, err := s.GetJob(caseID, jobID)
	if err != nil {
		return nil, err
	}
	switch job.Status {
	case model.VerifyStatusQueued, model.VerifyStatusRunning:
		return nil, kindErr(KindConflict, "job %d is already %s", job.ID, job.Status)
	case model.VerifyStatusVerified, model.VerifyStatusFailed:
		return nil, kindErr(KindConflict, "job %d is already %s and cannot be resumed", job.ID, job.Status)
	case model.VerifyStatusInterrupted:
	default:
		return nil, kindErr(KindConflict, "job %d is in state %s", job.ID, job.Status)
	}
	s.enqueue(job.ID)
	return job, nil
}

// GetJob 查询复核作业与进度。
func (s *Service) GetJob(caseID, jobID uint) (*model.VerifyJob, []model.VerifyChunk, error) {
	var job model.VerifyJob
	if err := s.db.Where("case_id = ? AND id = ?", caseID, jobID).First(&job).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, nil, kindErr(KindNotFound, "verification job %d not found", jobID)
		}
		return nil, nil, err
	}
	var chunks []model.VerifyChunk
	if err := s.db.Where("job_id = ?", job.ID).Order("offset ASC").Find(&chunks).Error; err != nil {
		return nil, nil, err
	}
	return &job, chunks, nil
}

// ListJobs 列出案件下全部复核作业。
func (s *Service) ListJobs(caseID uint) ([]model.VerifyJob, error) {
	if _, err := s.GetCase(caseID); err != nil {
		return nil, err
	}
	var jobs []model.VerifyJob
	if err := s.db.Where("case_id = ?", caseID).Order("id DESC").Find(&jobs).Error; err != nil {
		return nil, err
	}
	return jobs, nil
}

// requeueInterrupted 服务启动时把 queued/interrupted 作业重新排队。
// running 状态只可能来自上次进程被强杀，统一转 interrupted 再排队。
func (s *Service) requeueInterrupted() error {
	if err := s.db.Model(&model.VerifyJob{}).
		Where("status = ?", model.VerifyStatusRunning).
		Update("status", model.VerifyStatusInterrupted).Error; err != nil {
		return err
	}
	var ids []uint
	if err := s.db.Model(&model.VerifyJob{}).
		Where("status IN ?", []string{model.VerifyStatusQueued, model.VerifyStatusInterrupted}).
		Pluck("id", &ids).Error; err != nil {
		return err
	}
	for _, id := range ids {
		s.enqueue(id)
	}
	return nil
}

func (s *Service) enqueue(id uint) {
	s.mu.Lock()
	if s.enqueued[id] {
		s.mu.Unlock()
		return
	}
	s.enqueued[id] = true
	s.mu.Unlock()

	select {
	case s.queue <- id:
	default:
		// 队列容量耗尽时回退标记，稍后由状态补偿处理。
		s.mu.Lock()
		s.enqueued[id] = false
		s.mu.Unlock()
	}
}

func (s *Service) worker(n int) {
	defer s.wg.Done()
	for {
		select {
		case <-s.ctx.Done():
			return
		case id := <-s.queue:
			s.runJob(id)
			s.mu.Lock()
			s.enqueued[id] = false
			s.mu.Unlock()
		}
	}
}

// failJob 在单个事务内把作业标记失败并追加 verify(failed) 事件，
// 避免“状态已更新但链事件未落库”的不一致。
func (s *Service) failJob(job *model.VerifyJob, ev *model.Evidence, reason, actual string) {
	now := time.Now().UTC()
	job.Status = model.VerifyStatusFailed
	job.LastError = reason
	job.FinalSHA256 = actual
	job.FinishedAt = &now

	payload := VerifyPayload{
		JobID:       job.ID,
		EvidenceID:  ev.ID,
		Result:      model.VerifyStatusFailed,
		ExpectedSHA: ev.SHA256,
		ActualSHA:   actual,
		Size:        ev.Size,
		Reason:      reason,
	}
	_ = s.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Model(&model.VerifyJob{}).Where("id = ?", job.ID).
			Updates(map[string]any{
				"status": model.VerifyStatusFailed, "last_error": reason,
				"final_sha256": actual, "finished_at": &now,
			}).Error; err != nil {
			return err
		}
		_, err := chain.Append(tx, chain.AppendInput{
			CaseID: job.CaseID, Type: model.EventVerify,
			Actor: "system", Payload: mustJSON(payload),
		})
		return err
	})
}

// runJob 执行（或恢复）一个复核作业。
func (s *Service) runJob(jobID uint) {
	if s.ctx.Err() != nil {
		return
	}
	var job model.VerifyJob
	if err := s.db.First(&job, jobID).Error; err != nil {
		return
	}
	if job.Status == model.VerifyStatusVerified || job.Status == model.VerifyStatusFailed {
		return
	}

	var ev model.Evidence
	if err := s.db.First(&ev, job.EvidenceID).Error; err != nil {
		return
	}
	root, ok := s.roots[ev.RootName]
	if !ok {
		s.failJob(&job, &ev, fmt.Sprintf("whitelist root %q is not mounted", ev.RootName), "")
		return
	}

	// —— 恢复前/开始前身份验证：必须是同一个 inode 且大小一致。
	//    绝不能只看文件名或大小。
	fd, st, err := root.Open(ev.RelPath, unix.O_RDONLY)
	if err != nil {
		s.failJob(&job, &ev, fmt.Sprintf("reopen evidence: %v", err), "")
		return
	}
	defer unix.Close(fd)

	if st.Dev != ev.FileDev || st.Ino != ev.FileIno {
		s.failJob(&job, &ev, fmt.Sprintf(
			"file identity changed: expected dev=%d ino=%d, got dev=%d ino=%d (path replaced)",
			ev.FileDev, ev.FileIno, st.Dev, st.Ino), "")
		return
	}
	if st.Size != ev.Size {
		s.failJob(&job, &ev, fmt.Sprintf(
			"file size changed: expected %d, got %d", ev.Size, st.Size), "")
		return
	}

	h := sha256.New()
	var existing []model.VerifyChunk
	resumed := job.ProcessedSize > 0
	if resumed {
		// —— 恢复：逐块重读“已处理部分”，重算块摘要并与持久化记录比对，
		//    同时把这些字节喂入最终整体 hasher。任何不一致即判定被篡改，
		//    不复用旧进度。
		if err := s.db.Where("job_id = ?", job.ID).
			Order("offset ASC").Find(&existing).Error; err != nil {
			s.failJob(&job, &ev, fmt.Sprintf("load chunks: %v", err), "")
			return
		}
		buf := make([]byte, job.ChunkSize)
		var expectOffset int64
		for _, c := range existing {
			if c.Offset != expectOffset {
				s.failJob(&job, &ev, fmt.Sprintf(
					"chunk table is non-contiguous at offset %d", expectOffset), "")
				return
			}
			part := buf[:c.Length]
			if err := hashfile.ReadChunkAt(fd, part, c.Offset); err != nil {
				s.failJob(&job, &ev, fmt.Sprintf("reread prefix: %v", err), "")
				return
			}
			got := strings.ToUpper(hashfile.HashBytes(part))
			if got != strings.ToUpper(c.SHA256) {
				s.failJob(&job, &ev, fmt.Sprintf(
					"processed prefix was modified before resume at offset %d "+
						"(stored chunk %s != current %s)", c.Offset, c.SHA256, got), "")
				return
			}
			h.Write(part)
			expectOffset += int64(c.Length)
		}
		if expectOffset != job.ProcessedSize {
			s.failJob(&job, &ev, fmt.Sprintf(
				"corrupt resume state: chunks cover %d bytes but job reports %d",
				expectOffset, job.ProcessedSize), "")
			return
		}
		// 已处理部分末尾之后若文件被覆盖但块摘要恰好（不可能）漏检，
		// 最终整体摘要仍会兜底暴露。
	}

	if err := s.db.Model(&job).Update("status", model.VerifyStatusRunning).Error; err != nil {
		return
	}

	// —— 处理未读尾部，每块持久化摘要与进度。
	buf := make([]byte, job.ChunkSize)
	offset := job.ProcessedSize
	failed := false
	for offset < ev.Size {
		if s.ctx.Err() != nil {
			s.markInterrupted(&job)
			return
		}
		n := int(ev.Size - offset)
		if n > job.ChunkSize {
			n = job.ChunkSize
		}
		part := buf[:n]
		if err := hashfile.ReadChunkAt(fd, part, offset); err != nil {
			s.failJob(&job, &ev, fmt.Sprintf("read tail: %v", err), "")
			return
		}
		chunkDigest := strings.ToUpper(hashfile.HashBytes(part))
		c := model.VerifyChunk{
			JobID: job.ID, Offset: offset, Length: n, SHA256: chunkDigest,
		}
		if err := s.db.Create(&c).Error; err != nil {
			s.failJob(&job, &ev, fmt.Sprintf("persist chunk: %v", err), "")
			return
		}
		h.Write(part)
		offset += int64(n)

		if err := s.db.Model(&job).Updates(map[string]any{
			"processed_size": offset,
		}).Error; err != nil {
			s.failJob(&job, &ev, fmt.Sprintf("persist progress: %v", err), "")
			return
		}

		if s.jobHook != nil {
			if err := s.jobHook(job.ID, offset, ev.Size); err != nil {
				// 钩子要求停止（测试中断或服务关闭）：保留进度，等待恢复。
				s.markInterrupted(&job)
				failed = true
				break
			}
		}
	}
	if failed {
		return
	}

	// —— 全部读完后再 fstat 一次，确保读取期间未被替换/截断。
	cur, err := hashfile.Fstat(fd)
	if err != nil {
		s.failJob(&job, &ev, fmt.Sprintf("final fstat: %v", err), "")
		return
	}
	if cur.Dev != ev.FileDev || cur.Ino != ev.FileIno || cur.Size != ev.Size {
		s.failJob(&job, &ev, fmt.Sprintf(
			"file changed during verification: now dev=%d ino=%d size=%d",
			cur.Dev, cur.Ino, cur.Size), "")
		return
	}

	final := strings.ToUpper(hex.EncodeToString(h.Sum(nil)))
	now := time.Now().UTC()
	if final != strings.ToUpper(ev.SHA256) {
		s.failJob(&job, &ev, fmt.Sprintf(
			"digest mismatch: image was modified after registration "+
				"(expected %s, got %s)", ev.SHA256, final), final)
		return
	}

	payload := VerifyPayload{
		JobID: job.ID, EvidenceID: ev.ID, Result: model.VerifyStatusVerified,
		ExpectedSHA: ev.SHA256, ActualSHA: final, Size: ev.Size, Resumed: resumed,
	}
	_ = s.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Model(&model.VerifyJob{}).Where("id = ?", job.ID).
			Updates(map[string]any{
				"status":       model.VerifyStatusVerified,
				"final_sha256": final,
				"last_error":   "",
				"finished_at":  &now,
			}).Error; err != nil {
			return err
		}
		_, err := chain.Append(tx, chain.AppendInput{
			CaseID: job.CaseID, Type: model.EventVerify,
			Actor: "system", Payload: mustJSON(payload),
		})
		return err
	})
}

// markInterrupted 保留已持久化的进度，等待服务重启或后续恢复。
func (s *Service) markInterrupted(job *model.VerifyJob) {
	_ = s.db.Model(&model.VerifyJob{}).Where("id = ?", job.ID).
		Update("status", model.VerifyStatusInterrupted).Error
}

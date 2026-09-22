// Package scheduler 运行周期性后台任务：
//   - auto_escalation：status=new 且创建超过 24 小时未确认的告警自动升级；
//   - pending_recovery：扫描 detected_at 为空的事件重新检测（重启/崩溃安全网）。
//
// 多实例安全：每个任务通过 scheduler_locks 行锁保证同一时刻只有一个执行者；
// 锁带 TTL，持锁实例宕机后其他实例可接管，任务全部幂等，可重复执行。
package scheduler

import (
	"context"
	"fmt"
	"log"
	"time"

	"activityguard/internal/config"
	"activityguard/internal/detection"
	"activityguard/internal/models"
	"activityguard/internal/util"

	"gorm.io/datatypes"
	"gorm.io/gorm"
)

type Scheduler struct {
	db     *gorm.DB
	engine *detection.Engine
	cfg    config.Config
	nodeID string
	tick   time.Duration
	now    func() time.Time
}

func New(gdb *gorm.DB, engine *detection.Engine, cfg config.Config) *Scheduler {
	return &Scheduler{
		db:     gdb,
		engine: engine,
		cfg:    cfg,
		nodeID: util.NewID(),
		tick:   time.Minute,
		now:    func() time.Time { return time.Now().UTC() },
	}
}

// Run 阻塞运行直到 ctx 取消。
func (s *Scheduler) Run(ctx context.Context) {
	log.Printf("scheduler: started node=%s tick=%s", s.nodeID, s.tick)
	// 启动后稍作延迟再跑首轮，给导入流量让路。
	select {
	case <-ctx.Done():
		return
	case <-time.After(5 * time.Second):
	}
	s.runOnce(ctx)
	t := time.NewTicker(s.tick)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			log.Printf("scheduler: stopped")
			return
		case <-t.C:
			s.runOnce(ctx)
		}
	}
}

// runOnce 执行一轮全部任务，错误只记录不中断调度循环。
func (s *Scheduler) runOnce(ctx context.Context) {
	if err := s.RunAutoEscalation(ctx); err != nil {
		log.Printf("scheduler: auto_escalation: %v", err)
	}
	if err := s.withLock(ctx, "pending_recovery", 5*time.Minute, s.recoverPending); err != nil {
		log.Printf("scheduler: pending_recovery: %v", err)
	}
}

type jobFn func(ctx context.Context) error

// withLock 获取命名锁后执行 job；拿不到锁（别的实例在跑）直接跳过。
func (s *Scheduler) withLock(ctx context.Context, name string, ttl time.Duration, job jobFn) error {
	now := s.now()
	// 插入未过期的锁行；已有未过期锁时插入 0 行。
	res := s.db.Exec(`
		INSERT INTO scheduler_locks (name, locked_by, locked_at, expires_at, heartbeat_at)
		SELECT ?, ?, ?, ?, ?
		WHERE NOT EXISTS (
			SELECT 1 FROM scheduler_locks WHERE name = ? AND expires_at > ?
		)`,
		name, s.nodeID, now, now.Add(ttl), now, name, now)
	if res.Error != nil {
		return res.Error
	}
	if res.RowsAffected == 0 {
		return nil // 其他实例正在执行
	}

	// 心跳续租：锁在本轮任务结束后立即释放，心跳只是保险。
	hbCtx, stopHB := context.WithCancel(ctx)
	go s.heartbeat(hbCtx, name, ttl)
	defer func() {
		stopHB()
		s.db.Exec("DELETE FROM scheduler_locks WHERE name = ? AND locked_by = ?", name, s.nodeID)
	}()

	return job(ctx)
}

func (s *Scheduler) heartbeat(ctx context.Context, name string, ttl time.Duration) {
	t := time.NewTicker(ttl / 3)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			now := s.now()
			s.db.Exec(`UPDATE scheduler_locks
				SET expires_at = ?, heartbeat_at = ?
				WHERE name = ? AND locked_by = ?`,
				now.Add(ttl), now, name, s.nodeID)
		}
	}
}

// RunAutoEscalation 在命名锁保护下执行一轮自动升级，供测试与手动触发复用。
// 拿不到锁（其他实例正在执行）时直接返回 nil。
func (s *Scheduler) RunAutoEscalation(ctx context.Context) error {
	return s.withLock(ctx, "auto_escalation", 2*time.Minute, s.autoEscalate)
}

// autoEscalate 把创建超过 24h 仍是 new 的告警升级为 escalated 并写审计。
func (s *Scheduler) autoEscalate(ctx context.Context) error {
	cutoff := s.now().Add(-24 * time.Hour)
	var alerts []models.Alert
	if err := s.db.Where("status = ? AND created_at < ?", models.AlertNew, cutoff).
		Limit(500).Find(&alerts).Error; err != nil {
		return err
	}
	if len(alerts) == 0 {
		return nil
	}
	for _, a := range alerts {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		err := s.db.Transaction(func(tx *gorm.DB) error {
			now := s.now()
			res := tx.Model(&models.Alert{}).
				Where("id = ? AND status = ? AND created_at < ?", a.ID, models.AlertNew, cutoff).
				Updates(map[string]any{"status": models.AlertEscalated, "acknowledged_at": now})
			if res.Error != nil {
				return res.Error
			}
			if res.RowsAffected == 0 {
				return nil // 已被人工确认或其他实例处理
			}
			detail := datatypes.JSON([]byte(fmt.Sprintf(
				`{"reason":"auto_escalation_24h","escalated_at":%q,"created_at":%q}`,
				now.Format(time.RFC3339), a.CreatedAt.Format(time.RFC3339))))
			return tx.Create(&models.AuditLog{
				ID:         util.NewID(),
				ActorName:  "system-scheduler",
				Action:     "alert.auto_escalate",
				TargetType: "alert",
				TargetID:   a.ID,
				Detail:     detail,
			}).Error
		})
		if err != nil {
			log.Printf("scheduler: escalate alert %s: %v", a.ID, err)
		}
	}
	log.Printf("scheduler: auto-escalated %d alert(s)", len(alerts))
	return nil
}

// recoverPending 捡取检测未完成的事件（重启/崩溃遗留）。
func (s *Scheduler) recoverPending(ctx context.Context) error {
	n, err := s.engine.ProcessPending(5000)
	if err != nil {
		return err
	}
	if n > 0 {
		log.Printf("scheduler: re-processed %d pending event(s)", n)
	}
	return nil
}

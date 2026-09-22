// Package detection 实现异常检测引擎：固定规则（下载突增、首次 USB、夜间活动）
// 与基于最近 30 天历史的 2.5 标准差统计规则。
//
// 设计要点：
//   - 事件以 detected_at 是否为空区分“待处理/已处理”，导入与检测同事务，
//     崩溃或重启后由 ProcessPending 重新捡取，事件不丢；
//   - 每个告警有唯一 dedup_key，乱序/延迟事件触发的补算与重复调度都以
//     upsert 方式重算受影响窗口，不产生重复告警；
//   - 告警固化 rule_version 与 evidence，保留完整告警依据；
//   - 所有统计的“本地日期/夜间窗口”按员工时区在 Go 内换算，不依赖 MySQL 时区表。
package detection

import (
	"errors"
	"fmt"
	"time"

	"activityguard/internal/config"
	"activityguard/internal/models"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

// ErrContentConflict 表示同一 event_id 上报了不同内容。
var ErrContentConflict = errors.New("same event id with different content")

type Engine struct {
	db  *gorm.DB
	cfg config.Config
	now func() time.Time
}

func New(gdb *gorm.DB, cfg config.Config) *Engine {
	return &Engine{db: gdb, cfg: cfg, now: func() time.Time { return time.Now().UTC() }}
}

// withClock 仅供测试注入固定时钟。
func (e *Engine) withClock(f func() time.Time) *Engine {
	e.now = f
	return e
}

// ProcessPending 处理所有 detected_at 为空的事件。
// 启动恢复与定时安全网共用本方法；重复执行幂等。
// limit 控制单轮处理上限，返回本轮处理的事件数。
func (e *Engine) ProcessPending(limit int) (int, error) {
	if limit <= 0 {
		limit = 5000
	}
	processed := 0
	const batch = 200
	for processed < limit {
		var ids []string
		if err := e.db.Model(&models.Event{}).
			Where("detected_at IS NULL").
			Order("occurred_at ASC").
			Limit(batch).
			Pluck("id", &ids).Error; err != nil {
			return processed, err
		}
		if len(ids) == 0 {
			break
		}
		var evs []models.Event
		if err := e.db.Where("id IN ?", ids).Find(&evs).Error; err != nil {
			return processed, err
		}
		n, err := e.ProcessEvents(evs)
		processed += n
		if err != nil {
			return processed, err
		}
	}
	return processed, nil
}

// ProcessEvents 是检测的统一入口：先确保事件已落库（detected_at 为空），
// 再按员工分组检测并在同事务内标记 detected_at。
//
// 事件必须已带 ID / content_hash。若某 event_id 已存在（幂等重复），
// 已存在行会被复用：相同内容跳过，不同内容返回冲突错误（正常导入流程
// 会提前拦截；这里是兜底）。
//
// 返回真正参与检测的事件（新插入或尚未处理的行）。
func (e *Engine) ProcessEvents(evs []models.Event) (int, error) {
	if len(evs) == 0 {
		return 0, nil
	}
	stored, err := e.persistPending(evs)
	if err != nil {
		return 0, err
	}
	if len(stored) == 0 {
		return 0, nil
	}

	byEmp := make(map[string][]models.Event)
	for _, ev := range stored {
		byEmp[ev.EmployeeID] = append(byEmp[ev.EmployeeID], ev)
	}

	done := 0
	for empID, list := range byEmp {
		if err := e.processEmployee(empID, list); err != nil {
			return done, fmt.Errorf("detect employee %s: %w", empID, err)
		}
		done += len(list)
	}
	return done, nil
}

// persistPending 插入尚未入库的事件；已存在且内容一致的 event_id 复用现有行
// （若该行尚未检测，则也纳入本轮）。
func (e *Engine) persistPending(evs []models.Event) ([]models.Event, error) {
	ids := make([]string, len(evs))
	for i, ev := range evs {
		ids[i] = ev.EventID
	}
	var existing []models.Event
	if err := e.db.Where("event_id IN ?", ids).Find(&existing).Error; err != nil {
		return nil, err
	}
	byID := make(map[string]models.Event, len(existing))
	for _, ex := range existing {
		byID[ex.EventID] = ex
	}

	toInsert := make([]models.Event, 0, len(evs))
	out := make([]models.Event, 0, len(evs))
	for _, ev := range evs {
		if ex, ok := byID[ev.EventID]; ok {
			if ex.ContentHash != ev.ContentHash {
				return nil, fmt.Errorf("event %s: %w", ev.EventID, ErrContentConflict)
			}
			if ex.DetectedAt == nil {
				out = append(out, ex)
			}
			continue
		}
		toInsert = append(toInsert, ev)
	}
	if len(toInsert) > 0 {
		if err := e.db.Clauses(clause.OnConflict{DoNothing: true}).Create(&toInsert).Error; err != nil {
			return nil, err
		}
		// INSERT IGNORE 下并发竞态可能丢行；回读 event_id 确认。
		var confirmed []models.Event
		if err := e.db.Where("event_id IN ?", ids).Find(&confirmed).Error; err != nil {
			return nil, err
		}
		confirmedSet := make(map[string]models.Event, len(confirmed))
		for _, c := range confirmed {
			confirmedSet[c.EventID] = c
		}
		for _, ev := range toInsert {
			got, ok := confirmedSet[ev.EventID]
			if !ok {
				continue
			}
			if got.ID == ev.ID && got.DetectedAt == nil {
				out = append(out, got)
			}
		}
	}
	return out, nil
}

func (e *Engine) processEmployee(empID string, evs []models.Event) (err error) {
	now := e.now()
	tx := e.db.Begin()
	defer func() {
		if r := recover(); r != nil {
			tx.Rollback()
			panic(r)
		}
	}()
	if tx.Error != nil {
		return tx.Error
	}
	defer func() {
		if err != nil {
			tx.Rollback()
		}
	}()

	var emp models.Employee
	if err = tx.Where("id = ?", empID).First(&emp).Error; err != nil {
		return err
	}
	rules, err := loadActiveRules(tx)
	if err != nil {
		return err
	}

	loc, lerr := time.LoadLocation(emp.Timezone)
	if lerr != nil {
		loc = time.UTC
	}

	minOcc, maxOcc := evs[0].OccurredAt, evs[0].OccurredAt
	ids := make([]string, 0, len(evs))
	for _, ev := range evs {
		if ev.OccurredAt.Before(minOcc) {
			minOcc = ev.OccurredAt
		}
		if ev.OccurredAt.After(maxOcc) {
			maxOcc = ev.OccurredAt
		}
		ids = append(ids, ev.ID)
	}

	if err = e.evalDownloadBurst(tx, &emp, rules, minOcc, maxOcc, now, loc); err != nil {
		return err
	}
	if err = e.evalFirstUSB(tx, &emp, rules, now); err != nil {
		return err
	}
	if err = e.evalNight(tx, &emp, rules, minOcc, maxOcc, now, loc); err != nil {
		return err
	}
	if err = e.evalZScore(tx, &emp, rules, minOcc, maxOcc, now, loc); err != nil {
		return err
	}

	if err = tx.Model(&models.Event{}).Where("id IN ?", ids).Update("detected_at", now).Error; err != nil {
		return err
	}
	return tx.Commit().Error
}

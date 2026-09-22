package integration_test

import (
	"context"
	"testing"
	"time"

	"activityguard/internal/detection"
	"activityguard/internal/models"
	"activityguard/internal/scheduler"
	"activityguard/internal/testsupport"
)

// TestPendingRecoveryAcrossRestart 模拟重启：直接落库 detected_at=NULL 的事件，
// ProcessPending（启动恢复路径）必须全部补算，不丢事件。
func TestPendingRecoveryAcrossRestart(t *testing.T) {
	env := testsupport.New(t)
	emp := env.EmployeeByEmail(t, "frank.zhao@example.com")
	t0 := time.Now().UTC().Add(-3 * time.Hour)

	for i := 0; i < 5; i++ {
		env.InsertPendingEvent(t, testsupport.NewEvent(
			"restart-"+pad3(i), models.EventUSB, emp.ID,
			t0.Add(time.Duration(i)*time.Minute), map[string]any{"serial": "X"}))
	}
	// “重启”：新 Engine 实例捡取 NULL 事件。
	eng := detection.New(env.GDB, env.Cfg)
	n, err := eng.ProcessPending(1000)
	if err != nil {
		t.Fatalf("process pending: %v", err)
	}
	if n != 5 {
		t.Fatalf("processed = %d, want 5", n)
	}
	var nullN int64
	env.GDB.Model(&models.Event{}).Where("detected_at IS NULL").Count(&nullN)
	if nullN != 0 {
		t.Fatalf("pending remaining = %d", nullN)
	}
	if n := env.CountAlerts(t, models.RuleFirstUSB); n != 1 {
		t.Fatalf("first usb alert = %d, want 1", n)
	}

	// 再跑一次必须幂等（重复调度安全网）。
	n2, err := eng.ProcessPending(1000)
	if err != nil {
		t.Fatalf("second pending run: %v", err)
	}
	if n2 != 0 {
		t.Fatalf("second run processed = %d, want 0", n2)
	}
	if n := env.CountAlerts(t, models.RuleFirstUSB); n != 1 {
		t.Fatalf("first usb alert after rerun = %d, want 1", n)
	}
}

// TestSchedulerAutoEscalationAndLock 验证：
//   - 新建超过 24h 未确认的告警被自动升级并写审计；
//   - 调度锁保证重复/并发执行时只有一个执行者；
//   - 已被调查员确认的告警不会被自动升级。
func TestSchedulerAutoEscalationAndLock(t *testing.T) {
	env := testsupport.New(t)
	eng := env.Engine()
	sched := scheduler.New(env.GDB, eng, env.Cfg)

	oldEmp := env.EmployeeByEmail(t, "frank.zhao@example.com")
	newEmp := env.EmployeeByEmail(t, "bob.li@example.com")

	mkAlert := func(empID, dedup string, createdAt time.Time, acked bool) {
		a := models.Alert{
			ID: dedup + "-id", RuleKey: models.RuleFirstUSB, RuleVersion: 1,
			EmployeeID: empID, DepartmentID: "000000000000000000000000d3",
			Severity: models.SeverityMedium, Status: models.AlertNew,
			Title: "t", DedupKey: dedup, Evidence: []byte("{}"),
			FirstSeenAt: createdAt, LastEventAt: createdAt, CreatedAt: createdAt, UpdatedAt: createdAt,
		}
		if empID == newEmp.ID {
			a.DepartmentID = "000000000000000000000000d1"
		}
		if acked {
			a.Status = models.AlertInvestigating
			a.AcknowledgedAt = &createdAt
		}
		if err := env.GDB.Create(&a).Error; err != nil {
			t.Fatalf("create alert: %v", err)
		}
	}

	now := time.Now().UTC()
	mkAlert(oldEmp.ID, "old-unacked", now.Add(-25*time.Hour), false)
	mkAlert(oldEmp.ID, "old-acked", now.Add(-25*time.Hour), true)
	mkAlert(newEmp.ID, "fresh", now.Add(-time.Hour), false)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// 并发执行两轮（模拟重复调度/多实例）：锁保证不产生重复审计或错误升级。
	done := make(chan error, 2)
	go func() { done <- sched.RunAutoEscalation(ctx) }()
	// 直接再调一次 withLock 保护下的任务，验证互斥（其中一个应跳过而非报错）。
	go func() { done <- sched.RunAutoEscalation(ctx) }()
	if err := <-done; err != nil {
		t.Fatalf("run1: %v", err)
	}
	if err := <-done; err != nil {
		t.Fatalf("run2: %v", err)
	}

	var escalated models.Alert
	if err := env.GDB.Where("dedup_key = ?", "old-unacked").First(&escalated).Error; err != nil {
		t.Fatalf("load: %v", err)
	}
	if escalated.Status != models.AlertEscalated || escalated.AcknowledgedAt == nil {
		t.Fatalf("old unacked status = %s ack=%v", escalated.Status, escalated.AcknowledgedAt)
	}
	var ack models.Alert
	env.GDB.Where("dedup_key = ?", "old-acked").First(&ack)
	if ack.Status != models.AlertInvestigating {
		t.Fatalf("manually acked alert changed to %s", ack.Status)
	}
	var fresh models.Alert
	env.GDB.Where("dedup_key = ?", "fresh").First(&fresh)
	if fresh.Status != models.AlertNew {
		t.Fatalf("fresh alert escalated too early: %s", fresh.Status)
	}

	var auditN int64
	env.GDB.Model(&models.AuditLog{}).Where("action = ?", "alert.auto_escalate").Count(&auditN)
	if auditN != 1 {
		t.Fatalf("auto-escalate audit rows = %d, want exactly 1 (lock must dedupe)", auditN)
	}

	// 锁应在任务结束后释放，后续轮次可重新获得。
	var n int64
	env.GDB.Model(&models.SchedulerLock{}).Count(&n)
	if n != 0 {
		t.Fatalf("locks left behind: %d", n)
	}
}

// Package seed 在首次启动时幂等写入演示用部门、员工、账号与 v1 检测规则。
// 已存在的数据（按主键/唯一键判断）不会被覆盖。
package seed

import (
	"encoding/json"
	"os"

	"activityguard/internal/auth"
	"activityguard/internal/models"
	"activityguard/internal/util"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

const (
	deptEng     = "000000000000000000000000d1"
	deptSales   = "000000000000000000000000d2"
	deptFinance = "000000000000000000000000d3"

	userAdmin    = "000000000000000000000000a1"
	userAnalyst1 = "000000000000000000000000a2"
	userAnalyst2 = "000000000000000000000000a3"
)

type ruleDef struct {
	Key, Name, Desc string
	Version         int
	Params          map[string]any
}

func defaultRules() []ruleDef {
	return []ruleDef{
		{
			Key: models.RuleDownloadBurst, Version: 1,
			Name: "10 分钟大量下载",
			Desc: "任意 10 分钟滚动窗口内文件下载超过 50 个（窗口按 10 分钟对齐的时间桶统计）",
			Params: map[string]any{
				"window_minutes": 10,
				// 触发条件为 count > threshold，即至少 51 个。
				"threshold": 50,
			},
		},
		{
			Key: models.RuleFirstUSB, Version: 1,
			Name:   "首次 USB 使用",
			Desc:   "员工有记录以来首次接入 USB 设备",
			Params: map[string]any{},
		},
		{
			Key: models.RuleNightActivity, Version: 1,
			Name: "夜间活动",
			Desc: "活动发生在员工所属时区本地时间 20:00 至次日 06:00（左闭右开）",
			Params: map[string]any{
				"start_hour": 20,
				"end_hour":   6,
			},
		},
		{
			Key: models.RuleZScore, Version: 1,
			Name: "30 天统计异常",
			Desc: "以最近 30 个历史自然日（按员工时区）的每日下载量为样本，当日下载量超过均值 2.5 个标准差；样本不足时不启用本规则",
			Params: map[string]any{
				"lookback_days":     30,
				"z_threshold":       2.5,
				"min_sample_days":   7,
				"min_positive_days": 5,
				"metric":            "file_download_daily",
			},
		},
	}
}

// Run 幂等执行种子写入。
func Run(gdb *gorm.DB) error {
	adminPW := getenv("SEED_ADMIN_PASSWORD", "admin12345")
	analystPW := getenv("SEED_ANALYST_PASSWORD", "analyst12345")

	adminHash, err := auth.HashPassword(adminPW)
	if err != nil {
		return err
	}
	analystHash, err := auth.HashPassword(analystPW)
	if err != nil {
		return err
	}

	departments := []models.Department{
		{ID: deptEng, Name: "Engineering"},
		{ID: deptSales, Name: "Sales"},
		{ID: deptFinance, Name: "Finance"},
	}
	employees := []models.Employee{
		{ID: "000000000000000000000000e1", DepartmentID: deptEng, Name: "Alice Chen", Email: "alice.chen@example.com", Timezone: "America/New_York", IsActive: true},
		{ID: "000000000000000000000000e2", DepartmentID: deptEng, Name: "Bob Li", Email: "bob.li@example.com", Timezone: "Asia/Shanghai", IsActive: true},
		{ID: "000000000000000000000000e3", DepartmentID: deptEng, Name: "Carla Rossi", Email: "carla.rossi@example.com", Timezone: "Europe/Rome", IsActive: true},
		{ID: "000000000000000000000000e4", DepartmentID: deptSales, Name: "David Kim", Email: "david.kim@example.com", Timezone: "Asia/Seoul", IsActive: true},
		{ID: "000000000000000000000000e5", DepartmentID: deptSales, Name: "Emma Wang", Email: "emma.wang@example.com", Timezone: "America/Los_Angeles", IsActive: true},
		{ID: "000000000000000000000000e6", DepartmentID: deptFinance, Name: "Frank Zhao", Email: "frank.zhao@example.com", Timezone: "UTC", IsActive: true},
	}
	users := []models.User{
		{ID: userAdmin, Username: "admin", PasswordHash: adminHash, Role: "admin", IsActive: true},
		{ID: userAnalyst1, Username: "analyst_eng", PasswordHash: analystHash, Role: "analyst", IsActive: true},
		{ID: userAnalyst2, Username: "analyst_sales", PasswordHash: analystHash, Role: "analyst", IsActive: true},
	}
	assignments := []models.UserDepartment{
		{UserID: userAnalyst1, DepartmentID: deptEng},
		{UserID: userAnalyst2, DepartmentID: deptSales},
		{UserID: userAnalyst2, DepartmentID: deptFinance},
	}

	if err := upsertAll(gdb, departments); err != nil {
		return err
	}
	if err := upsertAll(gdb, employees); err != nil {
		return err
	}
	if err := upsertAll(gdb, users); err != nil {
		return err
	}
	if err := upsertAll(gdb, assignments); err != nil {
		return err
	}

	for _, rd := range defaultRules() {
		params, _ := json.Marshal(rd.Params)
		rule := models.DetectionRule{
			ID:          util.NewID(),
			RuleKey:     rd.Key,
			Version:     rd.Version,
			Name:        rd.Name,
			Description: rd.Desc,
			Params:      params,
			IsActive:    true,
		}
		// 同一 (rule_key, version) 已存在则跳过；激活版本由服务层保证唯一。
		if err := gdb.Clauses(clause.OnConflict{
			Columns:   []clause.Column{{Name: "rule_key"}, {Name: "version"}},
			DoNothing: true,
		}).Create(&rule).Error; err != nil {
			return err
		}
	}
	return nil
}

func upsertAll[T any](gdb *gorm.DB, rows []T) error {
	if len(rows) == 0 {
		return nil
	}
	return gdb.Clauses(clause.OnConflict{UpdateAll: false, DoNothing: true}).Create(&rows).Error
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

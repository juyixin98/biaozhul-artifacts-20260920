// Package seed 写入演示用用户与默认检查清单（幂等，可随容器启动执行）。
package seed

import (
	"time"

	"gorm.io/gorm"

	"proofcycle/internal/domain"
)

// 固定 ID 便于演示脚本和文档引用。
const (
	UserDesigner = "u-designer"
	UserPM       = "u-pm"
	Reviewer1    = "u-reviewer-1"
	Reviewer2    = "u-reviewer-2"
	Reviewer3    = "u-reviewer-3"
	Reviewer4    = "u-reviewer-4"
	Reviewer5    = "u-reviewer-5"
	Reviewer6    = "u-reviewer-6"
	Reviewer7    = "u-reviewer-7"
	Reviewer8    = "u-reviewer-8"

	DefaultChecklist = "cl-packaging-default"
)

// Ensure 幂等写入演示用户与默认检查清单。
func Ensure(db *gorm.DB) error {
	now := time.Now().UTC()
	users := []domain.User{
		{ID: UserDesigner, Name: "设计师 小林", Role: domain.RoleDesigner, CreatedAt: now},
		{ID: UserPM, Name: "项目经理 老周", Role: domain.RolePM, CreatedAt: now},
		{ID: Reviewer1, Name: "审查员 A（结构）", Role: domain.RoleReviewer, CreatedAt: now},
		{ID: Reviewer2, Name: "审查员 B（色彩）", Role: domain.RoleReviewer, CreatedAt: now},
		{ID: Reviewer3, Name: "审查员 C（文字）", Role: domain.RoleReviewer, CreatedAt: now},
		{ID: Reviewer4, Name: "审查员 D（条码）", Role: domain.RoleReviewer, CreatedAt: now},
		{ID: Reviewer5, Name: "审查员 E（材料）", Role: domain.RoleReviewer, CreatedAt: now},
		{ID: Reviewer6, Name: "审查员 F（法规标识）", Role: domain.RoleReviewer, CreatedAt: now},
		{ID: Reviewer7, Name: "审查员 G（尺寸刀版）", Role: domain.RoleReviewer, CreatedAt: now},
		{ID: Reviewer8, Name: "审查员 H（物流适配）", Role: domain.RoleReviewer, CreatedAt: now},
	}
	for _, u := range users {
		if err := db.Where("id = ?", u.ID).Assign(u).FirstOrCreate(&domain.User{}).Error; err != nil {
			return err
		}
	}

	ci := domain.Checklist{
		ID: DefaultChecklist, Name: "包装打样默认检查清单（v1）", Version: 1, CreatedAt: now,
	}
	if err := db.Where("id = ?", ci.ID).Assign(ci).FirstOrCreate(&domain.Checklist{}).Error; err != nil {
		return err
	}
	items := []domain.ChecklistItem{
		{OrderNo: 1, Code: "COLOR", Text: "印刷颜色与签样色卡色差在允许范围内（ΔE 判定）"},
		{OrderNo: 2, Code: "RESOLUTION", Text: "图片分辨率不低于 300DPI，无可见锯齿与糊版"},
		{OrderNo: 3, Code: "TEXT", Text: "品牌名、卖点、净含量等文字无错漏字，字体已嵌入/转曲"},
		{OrderNo: 4, Code: "BARCODE", Text: "商品条码可扫读，等级不低于 C，留白与缩放比例正确"},
		{OrderNo: 5, Code: "MATERIAL", Text: "纸张克重、覆膜与工艺和工艺单一致"},
		{OrderNo: 6, Code: "LABEL", Text: "产品名称、规格、批号、日期等信息位置预留充足"},
		{OrderNo: 7, Code: "DIE", Text: "刀版、出血、折叠线标注与样盒一致"},
		{OrderNo: 8, Code: "LOGISTICS", Text: "外箱装箱数、堆码与运输标识与文件一致"},
	}
	for _, it := range items {
		it.ID = ci.ID + "-" + it.Code
		it.ChecklistID = ci.ID
		if err := db.Where("id = ?", it.ID).Assign(it).FirstOrCreate(&domain.ChecklistItem{}).Error; err != nil {
			return err
		}
	}
	return nil
}

// AllReviewers 返回演示审查员 ID。
func AllReviewers() []string {
	return []string{Reviewer1, Reviewer2, Reviewer3, Reviewer4, Reviewer5, Reviewer6, Reviewer7, Reviewer8}
}

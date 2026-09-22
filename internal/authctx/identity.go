// Package authctx 保存请求上下文中的登录身份。
package authctx

type Identity struct {
	UserID   string
	Username string
	Role     string // admin | analyst
	// DepartmentIDs 为分析员被分配可见的部门 ID；admin 为 nil（表示全部）。
	DepartmentIDs []string
}

func (i Identity) IsAdmin() bool { return i.Role == "admin" }

// CanSeeDepartment 判定身份是否可访问某部门数据。
func (i Identity) CanSeeDepartment(deptID string) bool {
	if i.IsAdmin() {
		return true
	}
	for _, d := range i.DepartmentIDs {
		if d == deptID {
			return true
		}
	}
	return false
}

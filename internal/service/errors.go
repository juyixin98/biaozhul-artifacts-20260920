package service

import "strings"

// isDuplicateKey 判断 MySQL 唯一键冲突（错误码 1062），避免直接依赖驱动包。
func isDuplicateKey(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	return strings.Contains(msg, "1062") || strings.Contains(msg, "Duplicate entry")
}

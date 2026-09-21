// Package httpapi 提供 ProofCycle 的 REST API（Gin）。
package httpapi

import (
	"errors"
	"fmt"
	"net/http"

	"github.com/gin-gonic/gin"

	"proofcycle/internal/service"
)

// 简单的演示认证：请求头 X-User-Id 携带用户 ID（容器内/本地演示用）。
const userHeader = "X-User-Id"

// authUser 是中间件写入上下文的当前用户。
type authUser struct {
	ID   string
	Name string
	Role string
}

func currentUser(c *gin.Context) authUser {
	v, _ := c.Get("user")
	return v.(authUser)
}

// authRequired 解析 X-User-Id 并校验用户存在。生产环境应替换为正式鉴权。
func (a *API) authRequired() gin.HandlerFunc {
	return func(c *gin.Context) {
		id := c.GetHeader(userHeader)
		if id == "" {
			fail(c, http.StatusUnauthorized, "missing %s header", userHeader)
			c.Abort()
			return
		}
		u, err := a.svc.User.Get(id)
		if err != nil {
			if errors.Is(err, service.ErrUnknownUser) {
				fail(c, http.StatusUnauthorized, "unknown user id")
				c.Abort()
				return
			}
			fail(c, http.StatusInternalServerError, "auth failed")
			c.Abort()
			return
		}
		c.Set("user", authUser{ID: u.ID, Name: u.Name, Role: string(u.Role)})
		c.Next()
	}
}

// fail 输出统一错误体。
func fail(c *gin.Context, status int, format string, args ...any) {
	c.AbortWithStatusJSON(status, gin.H{"error": fmt.Sprintf(format, args...)})
}

// mapServiceError 把服务层错误映射为 HTTP 状态码。
func mapServiceError(c *gin.Context, err error) {
	switch {
	case errors.Is(err, service.ErrNotFound):
		fail(c, http.StatusNotFound, "%s", err.Error())
	case errors.Is(err, service.ErrUnknownUser), errors.Is(err, service.ErrVersionMissing),
		errors.Is(err, service.ErrUnknownChecklist):
		fail(c, http.StatusNotFound, "%s", err.Error())
	case errors.Is(err, service.ErrForbidden),
		errors.Is(err, service.ErrNotPM),
		errors.Is(err, service.ErrNotDesigner),
		errors.Is(err, service.ErrNotAssignedReviewer),
		errors.Is(err, service.ErrDesignerCannotApprove):
		fail(c, http.StatusForbidden, "%s", err.Error())
	case errors.Is(err, service.ErrStaleVersion), errors.Is(err, service.ErrConflict),
		errors.Is(err, service.ErrAlreadyApproved):
		fail(c, http.StatusConflict, "%s", err.Error())
	case errors.Is(err, service.ErrValidation),
		errors.Is(err, service.ErrTooManyReviewers),
		errors.Is(err, service.ErrReviewerMissing),
		errors.Is(err, service.ErrDistinctMembers),
		errors.Is(err, service.ErrDuplicateReviewer),
		errors.Is(err, service.ErrFailReasonRequired),
		errors.Is(err, service.ErrUnknownSnapshotItem),
		errors.Is(err, service.ErrInvalidResult):
		fail(c, http.StatusBadRequest, "%s", err.Error())
	case errors.Is(err, service.ErrNotAllReviewers),
		errors.Is(err, service.ErrIncompleteReview),
		errors.Is(err, service.ErrUnprocessedItems),
		errors.Is(err, service.ErrFailedItems):
		// 业务规则拦截（存在未处理/失败项、审查员未齐）：语义为冲突不可签核。
		fail(c, http.StatusConflict, "%s", err.Error())
	default:
		fail(c, http.StatusInternalServerError, "internal error: %s", err.Error())
	}
}

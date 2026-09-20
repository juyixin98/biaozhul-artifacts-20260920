package httpapi

import (
	"errors"
	"log"
	"net/http"

	"github.com/gin-gonic/gin"

	"proofcycle/internal/service"
)

func abortServiceError(c *gin.Context, err error) {
	var se *service.Error
	if errors.As(err, &se) {
		body := gin.H{"code": se.Code, "message": se.Message}
		if se.Details != nil {
			body["details"] = se.Details
		}
		c.AbortWithStatusJSON(se.Status, body)
		return
	}
	log.Printf("internal error: %v", err)
	c.AbortWithStatusJSON(http.StatusInternalServerError, gin.H{
		"code": "internal_error", "message": "internal server error",
	})
}

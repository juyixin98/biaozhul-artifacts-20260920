package api

import (
	"errors"
	"log"
	"net/http"
	"os"
	"time"

	"forensiccore/internal/cases"
	"forensiccore/internal/chain"
	"forensiccore/internal/evidence"
	"forensiccore/internal/review"
	"forensiccore/internal/securefile"

	"github.com/gin-gonic/gin"
)

func requestLogger() gin.HandlerFunc {
	return func(c *gin.Context) {
		start := time.Now()
		c.Next()
		if c.Request.URL.Path == "/healthz" {
			return
		}
		log.Printf("[api] %s %s -> %d (%s)",
			c.Request.Method, c.Request.URL.Path, c.Writer.Status(), time.Since(start))
	}
}

// mapError translates domain errors into HTTP status codes.
func mapError(c *gin.Context, err error) {
	switch {
	case errors.Is(err, cases.ErrNotFound), errors.Is(err, evidence.ErrNotFound),
		errors.Is(err, review.ErrNotFound), errors.Is(err, chain.ErrNotFound):
		c.JSON(http.StatusNotFound, gin.H{"error": err.Error()})
	case errors.Is(err, chain.ErrCaseNotFound), errors.Is(err, evidence.ErrCaseNotFound):
		c.JSON(http.StatusNotFound, gin.H{"error": err.Error()})
	case errors.Is(err, evidence.ErrAlreadyExists):
		c.JSON(http.StatusConflict, gin.H{"error": err.Error()})
	case errors.Is(err, review.ErrAlreadyRunning):
		c.JSON(http.StatusConflict, gin.H{"error": err.Error()})
	case errors.Is(err, securefile.ErrOutsideWhitelist), errors.Is(err, securefile.ErrSymlinkEscape):
		c.JSON(http.StatusForbidden, gin.H{"error": err.Error()})
	case errors.Is(err, securefile.ErrUnsupportedType), errors.Is(err, securefile.ErrNotRegularFile):
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
	case errors.Is(err, securefile.ErrFileChanged):
		c.JSON(http.StatusConflict, gin.H{"error": err.Error()})
	case errors.Is(err, review.ErrIdentityMismatch), errors.Is(err, review.ErrPrefixMismatch),
		errors.Is(err, review.ErrSizeChanged):
		c.JSON(http.StatusConflict, gin.H{"error": err.Error()})
	case errors.Is(err, cases.ErrInvalidID):
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
	case errors.Is(err, os.ErrNotExist):
		c.JSON(http.StatusBadRequest, gin.H{"error": "evidence file does not exist or is not accessible: " + err.Error()})
	default:
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
	}
}

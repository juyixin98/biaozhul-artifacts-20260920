package api

import (
	"errors"
	"net/http"

	"proofcycle/internal/auth"
	"proofcycle/internal/middleware"
	"proofcycle/internal/models"
	"proofcycle/internal/service"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

// Handler wires the service to HTTP routes.
type Handler struct {
	db       *gorm.DB
	svc      *service.Service
	maxBytes int64
}

func New(gdb *gorm.DB, svc *service.Service, maxUploadBytes int64) *Handler {
	return &Handler{db: gdb, svc: svc, maxBytes: maxUploadBytes}
}

// Router builds the gin engine with all routes.
func (h *Handler) Router() *gin.Engine {
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(requestLogger())
	// Multipart metadata is buffered in memory; file payloads stream to disk
	// through the storage layer's size-enforcing reader.
	r.MaxMultipartMemory = 8 << 20
	r.Use(limitBody(h.maxBytes + (10 << 20)))

	r.GET("/healthz", h.health)

	v1 := r.Group("/api/v1")
	{
		v1.POST("/login", h.login)

		authed := v1.Group("")
		authed.Use(middleware.AuthRequired(h.db))
		{
			authed.POST("/logout", h.logout)
			authed.GET("/me", h.me)

			authed.POST("/jobs", h.createJob)
			authed.GET("/jobs/:id", h.getJob)
			authed.GET("/jobs", h.listJobs)

			authed.POST("/jobs/:id/versions", h.uploadVersion)
			authed.GET("/jobs/:id/versions", h.listVersions)
			authed.GET("/jobs/:id/versions/:ver", h.getVersion)
			authed.GET("/jobs/:id/versions/:ver/download", h.downloadVersion)
			authed.POST("/jobs/:id/revisions", h.uploadRevision)

			authed.GET("/jobs/:id/rounds/active", h.activeRound)
			authed.GET("/jobs/:id/history", h.fullHistory)
			authed.GET("/jobs/:id/versions/:ver/history", h.versionHistory)

			authed.PUT("/jobs/:id/opinions", h.upsertOpinions)
			authed.POST("/jobs/:id/approvals", h.approve)
			authed.GET("/jobs/:id/versions/:ver/report", h.versionReport)
			authed.GET("/jobs/:id/report", h.latestReport)
		}
	}
	return r
}

func (h *Handler) health(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"status": "ok"})
}

type loginReq struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

func (h *Handler) login(c *gin.Context) {
	var req loginReq
	if err := c.ShouldBindJSON(&req); err != nil {
		fail(c, http.StatusBadRequest, "invalid JSON body")
		return
	}
	u, token, err := auth.Login(h.db, req.Username, req.Password)
	if err != nil {
		fail(c, http.StatusUnauthorized, err.Error())
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"token": token,
		"user":  userView(u),
	})
}

func (h *Handler) logout(c *gin.Context) {
	u := middleware.User(c)
	if err := auth.Logout(h.db, u.ID); err != nil {
		fail(c, http.StatusInternalServerError, "failed to log out")
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": "logged out"})
}

func (h *Handler) me(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"user": userView(middleware.User(c))})
}

// writeServiceError maps service errors onto HTTP status codes. Unknown errors
// are masked as 500 to avoid leaking internals.
func writeServiceError(c *gin.Context, err error) {
	switch {
	case errors.Is(err, gorm.ErrRecordNotFound), errors.Is(err, service.ErrNotFound):
		fail(c, http.StatusNotFound, "not found")
	case errors.Is(err, service.ErrVersionMismatch):
		fail(c, http.StatusNotFound, err.Error())
	case errors.Is(err, service.ErrForbidden):
		// Non-members get 404 so job existence is never disclosed.
		fail(c, http.StatusNotFound, "not found")
	case errors.Is(err, service.ErrRoleDenied),
		errors.Is(err, service.ErrNotAssignedReviewer),
		errors.Is(err, service.ErrNotApprover):
		fail(c, http.StatusForbidden, err.Error())
	case errors.Is(err, service.ErrSelfApproval):
		fail(c, http.StatusForbidden, err.Error())
	case errors.Is(err, service.ErrConflict):
		fail(c, http.StatusConflict, err.Error())
	case errors.Is(err, service.ErrRoundInactive),
		errors.Is(err, service.ErrJobClosed):
		fail(c, http.StatusConflict, err.Error())
	case errors.Is(err, service.ErrFileTooLarge):
		fail(c, http.StatusRequestEntityTooLarge, err.Error())
	case errors.Is(err, service.ErrFileType):
		fail(c, http.StatusUnsupportedMediaType, err.Error())
	case errors.Is(err, service.ErrInvalidInput),
		errors.Is(err, service.ErrReviewerRoster),
		errors.Is(err, service.ErrReviewerRole),
		errors.Is(err, service.ErrDesignerConflict),
		errors.Is(err, service.ErrPMConflict),
		errors.Is(err, service.ErrFailReasonRequired),
		errors.Is(err, service.ErrUnknownItem),
		errors.Is(err, service.ErrOutcomeInvalid):
		fail(c, http.StatusBadRequest, err.Error())
	case errors.Is(err, service.ErrPendingItems),
		errors.Is(err, service.ErrFailedItems),
		errors.Is(err, service.ErrReviewersIncomplete):
		fail(c, http.StatusUnprocessableEntity, err.Error())
	default:
		_ = c.Error(err)
		fail(c, http.StatusInternalServerError, "internal server error")
	}
}

func fail(c *gin.Context, status int, msg string) {
	c.AbortWithStatusJSON(status, gin.H{"error": msg})
}

func userView(u *models.User) gin.H {
	return gin.H{"id": u.ID, "username": u.Username, "role": u.Role}
}

func requestLogger() gin.HandlerFunc {
	return gin.Logger()
}

// limitBody caps the total request body so an oversized upload is rejected
// before streaming (the storage layer enforces the precise file limit).
func limitBody(max int64) gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, max)
		c.Next()
	}
}

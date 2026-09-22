package api

import (
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"geoterritory/geometry"
	"geoterritory/internal/models"
	"geoterritory/internal/store"
)

// resolveCatalogVersion translates the ?catalog_version= query into a concrete
// version number.
//
//   - absent or "current" -> current_version (queries and assignments);
//   - "published"        -> published_version (the new catalog while a
//     reassignment is still running);
//   - integer            -> that exact historical version.
//
// Assignments are retained for every version, so historical reads stay
// consistent after a catalog flip.
func resolveCatalogVersion(c *gin.Context, st *models.CatalogState) (int, bool) {
	v := c.DefaultQuery("catalog_version", "current")
	switch v {
	case "", "current":
		return st.CurrentVersion, true
	case "published":
		return st.PublishedVersion, true
	}
	n, err := atoiPositive(v)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "catalog_version must be 'current', 'published' or a non-negative integer"})
		return 0, false
	}
	return n, true
}

type vertexDTO struct {
	Lat float64 `json:"lat"`
	Lng float64 `json:"lng"`
}

type publishRegionRequest struct {
	Name     string      `json:"name"`
	Priority int         `json:"priority"`
	Vertices []vertexDTO `json:"vertices"`
}

func (s *Server) publishRegion(c *gin.Context) {
	org := orgFrom(c)
	var req publishRegionRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid JSON body: " + err.Error()})
		return
	}
	if len(req.Name) == 0 || len(req.Name) > 128 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "name required (max 128 chars)"})
		return
	}
	if req.Priority < 1 || req.Priority > 5 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "priority must be between 1 and 5 (1 wins overlaps)"})
		return
	}
	if len(req.Vertices) < 3 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "a closed polygon needs at least 3 vertices (do not repeat the first vertex as the last)"})
		return
	}
	if len(req.Vertices) > 10000 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "too many vertices (max 10000)"})
		return
	}
	ring := make([]geometry.LatLng, len(req.Vertices))
	for i, v := range req.Vertices {
		ring[i] = geometry.LatLng{Lat: v.Lat, Lng: v.Lng}
	}
	if err := geometry.ValidateRing(ring); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid polygon", "detail": err.Error()})
		return
	}

	var catalogVersion int
	var jobID uint64
	err := store.WithOrgLock(s.db, org.ID, s.cfg.LockTimeoutS, func(tx *gorm.DB) error {
		var e error
		catalogVersion, jobID, e = store.PublishRegion(tx, org.ID, store.CreateRegionInput{
			Name:     req.Name,
			Priority: req.Priority,
			Vertices: ringAsModels(ring),
		})
		return e
	})
	if errors.Is(err, store.ErrActiveJob) {
		c.JSON(http.StatusConflict, gin.H{"error": "a reassignment job is already active for this organization; wait for it to finish"})
		return
	}
	if errors.Is(err, store.ErrLockBusy) {
		c.JSON(http.StatusConflict, gin.H{"error": "organization is busy with another mutating operation, retry shortly"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusAccepted, gin.H{
		"catalog_version": catalogVersion,
		"job_id":          jobID,
		"status":          "PUBLISHED_PENDING_REASSIGNMENT",
		"message":         "new catalog published; current_version flips atomically once reassignment completes",
	})
}

func ringAsModels(ring []geometry.LatLng) models.Vertices {
	v := make(models.Vertices, len(ring))
	for i, p := range ring {
		v[i] = p
	}
	return v
}

func (s *Server) listRegions(c *gin.Context) {
	org := orgFrom(c)
	activeOnly := c.Query("active") == "true"
	regions, err := store.ListRegions(s.db, org.ID, activeOnly)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"regions": regions})
}

func (s *Server) getRegion(c *gin.Context) {
	org := orgFrom(c)
	id, ok := parseUintParam(c, "id")
	if !ok {
		return
	}
	r, err := store.GetRegion(s.db, org.ID, id)
	if errors.Is(err, store.ErrNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "region not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	rv, err := store.LatestRegionVersion(s.db, r.ID)
	if err == nil {
		c.JSON(http.StatusOK, gin.H{"region": r, "latest": rv})
		return
	}
	c.JSON(http.StatusOK, gin.H{"region": r})
}

func (s *Server) getRegionVersion(c *gin.Context) {
	org := orgFrom(c)
	id, ok := parseUintParam(c, "id")
	if !ok {
		return
	}
	version, ok := parseVersionParam(c)
	if !ok {
		return
	}
	rv, err := store.GetRegionVersion(s.db, org.ID, id, version)
	if errors.Is(err, store.ErrNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "region version not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"region_version": rv})
}

func (s *Server) deactivateRegion(c *gin.Context) {
	org := orgFrom(c)
	id, ok := parseUintParam(c, "id")
	if !ok {
		return
	}
	err := store.WithOrgLock(s.db, org.ID, s.cfg.LockTimeoutS, func(tx *gorm.DB) error {
		return store.DeactivateRegion(tx, org.ID, id)
	})
	if errors.Is(err, store.ErrNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "region not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": "deactivated", "message": "region excluded from the next published catalog"})
}

func (s *Server) getCatalog(c *gin.Context) {
	org := orgFrom(c)
	st, err := store.CatalogStateRow(s.db, org.ID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	cat, err := store.LoadCatalog(s.db, org.ID, st.CurrentVersion)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"current_version":   st.CurrentVersion,
		"published_version": st.PublishedVersion,
		"regions":           cat.Entries(),
		"reassigning":       st.CurrentVersion != st.PublishedVersion,
	})
}

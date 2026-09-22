package api

import (
	"errors"
	"math"
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"

	"geoterritory/geometry"
	"geoterritory/internal/store"
)

func atoiPositive(s string) (int, error) {
	n, err := strconv.Atoi(s)
	if err != nil || n < 0 {
		return 0, errors.New("non-negative integer required")
	}
	return n, nil
}

func parseUintParam(c *gin.Context, name string) (uint64, bool) {
	id, err := strconv.ParseUint(c.Param(name), 10, 64)
	if err != nil || id == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": name + " must be a positive integer"})
		return 0, false
	}
	return id, true
}

func parseVersionParam(c *gin.Context) (int, bool) {
	v := c.Param("version")
	n, err := strconv.Atoi(v)
	if err != nil || n < 1 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "version must be a positive integer"})
		return 0, false
	}
	return n, true
}

func parseFloatQuery(c *gin.Context, name string, dst *float64) bool {
	raw, ok := c.GetQuery(name)
	if !ok {
		c.JSON(http.StatusBadRequest, gin.H{"error": name + " query parameter required"})
		return false
	}
	v, err := strconv.ParseFloat(raw, 64)
	if err != nil || math.IsNaN(v) || math.IsInf(v, 0) {
		c.JSON(http.StatusBadRequest, gin.H{"error": name + " must be a finite number"})
		return false
	}
	*dst = v
	return true
}

// resolvedVersion loads the catalog pointers and resolves the requested
// version.
func (s *Server) resolvedVersion(c *gin.Context) (uint64, int, bool) {
	org := orgFrom(c)
	st, err := store.CatalogStateRow(s.db, org.ID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return org.ID, 0, false
	}
	v, ok := resolveCatalogVersion(c, st)
	if !ok {
		return org.ID, 0, false
	}
	if v > st.PublishedVersion {
		c.JSON(http.StatusNotFound, gin.H{"error": "catalog version does not exist"})
		return org.ID, 0, false
	}
	return org.ID, v, true
}

func (s *Server) getPoint(c *gin.Context) {
	orgID, ver, ok := s.resolvedVersion(c)
	if !ok {
		return
	}
	externalID := c.Param("external_id")
	if externalID == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "external_id required"})
		return
	}
	ap, err := store.GetPointByExternalID(s.db, orgID, externalID, ver)
	if errors.Is(err, store.ErrNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "point not found in catalog version"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"point": ap})
}

func (s *Server) bboxQuery(c *gin.Context) {
	orgID, ver, ok := s.resolvedVersion(c)
	if !ok {
		return
	}
	var f store.BBoxFilter
	if !parseFloatQuery(c, "min_lat", &f.MinLat) || !parseFloatQuery(c, "max_lat", &f.MaxLat) ||
		!parseFloatQuery(c, "min_lng", &f.MinLng) || !parseFloatQuery(c, "max_lng", &f.MaxLng) {
		return
	}
	if err := validateLat(f.MinLat); err != nil {
		badRequest(c, "min_lat", err)
		return
	}
	if err := validateLat(f.MaxLat); err != nil {
		badRequest(c, "max_lat", err)
		return
	}
	if err := validateLng(f.MinLng); err != nil {
		badRequest(c, "min_lng", err)
		return
	}
	if err := validateLng(f.MaxLng); err != nil {
		badRequest(c, "max_lng", err)
		return
	}
	if f.MinLat > f.MaxLat {
		c.JSON(http.StatusBadRequest, gin.H{"error": "min_lat must be <= max_lat"})
		return
	}
	// Explicit antimeridian policy: a box wrapping past +/-180 must not be
	// misinterpreted as the small complementary box.
	if f.MinLng > f.MaxLng {
		c.JSON(http.StatusBadRequest, gin.H{"error": "min_lng must be <= max_lng; antimeridian-crossing bounding boxes are not supported"})
		return
	}
	f.AssignedOnly = c.Query("assigned_only") == "true"
	f.Limit = 1000
	f.Offset = 0
	if v := c.Query("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 || n > 1000 {
			c.JSON(http.StatusBadRequest, gin.H{"error": "limit must be 1..1000"})
			return
		}
		f.Limit = n
	}
	if v := c.Query("offset"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			c.JSON(http.StatusBadRequest, gin.H{"error": "offset must be >= 0"})
			return
		}
		f.Offset = n
	}
	pts, err := store.BBoxQuery(s.db, orgID, ver, f)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"catalog_version": ver, "count": len(pts), "points": pts})
}

func badRequest(c *gin.Context, field string, err error) {
	c.JSON(http.StatusBadRequest, gin.H{"error": field + ": " + err.Error()})
}

func validateLat(v float64) error {
	if v < -90 || v > 90 {
		return errors.New("latitude out of range [-90, 90]")
	}
	return nil
}
func validateLng(v float64) error {
	if v < -180 || v > 180 {
		return errors.New("longitude out of range [-180, 180]")
	}
	return nil
}

func (s *Server) nearestQuery(c *gin.Context) {
	orgID, ver, ok := s.resolvedVersion(c)
	if !ok {
		return
	}
	var lat, lng float64
	if !parseFloatQuery(c, "lat", &lat) || !parseFloatQuery(c, "lng", &lng) {
		return
	}
	if err := geometry.ValidateCoordinate(geometry.LatLng{Lat: lat, Lng: lng}); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	n := 10
	if v := c.Query("n"); v != "" {
		x, err := strconv.Atoi(v)
		if err != nil || x < 1 {
			c.JSON(http.StatusBadRequest, gin.H{"error": "n must be a positive integer"})
			return
		}
		n = x
	}
	if n > 50 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "n must be at most 50", "max_n": 50})
		return
	}
	assignedOnly := c.Query("assigned_only") == "true"
	rows, err := store.NearestQuery(s.db, orgID, ver, lat, lng, n, assignedOnly)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"catalog_version": ver,
		"anchor":          gin.H{"lat": lat, "lng": lng},
		"count":           len(rows),
		"points":          rows,
	})
}

func (s *Server) getJob(c *gin.Context) {
	org := orgFrom(c)
	id, ok := parseUintParam(c, "id")
	if !ok {
		return
	}
	job, err := store.GetJob(s.db, org.ID, id)
	if errors.Is(err, store.ErrNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "job not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"job": job})
}

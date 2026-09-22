package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"

	"geoterritory/internal/geometry"
	"geoterritory/internal/models"
	"geoterritory/internal/service"

	"github.com/gin-gonic/gin"
)

// Handler wires the services to HTTP.
type Handler struct {
	regions *service.RegionService
	points  *service.PointService
}

func NewHandler(rs *service.RegionService, ps *service.PointService) *Handler {
	return &Handler{regions: rs, points: ps}
}

// Register mounts all authenticated routes.
func (h *Handler) Register(r gin.IRouter) {
	r.GET("/health", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"status": "ok"}) })

	r.GET("/regions", h.listRegions)
	r.POST("/regions", h.createRegion)
	r.GET("/regions/:id", h.getRegion)
	r.POST("/regions/:id/versions", h.publishVersion)
	r.GET("/jobs/:id", h.getJob)

	r.POST("/points/batch", h.batchImport)
	r.GET("/points/:external_id", h.getPoint)
	r.PATCH("/points/:external_id", h.updatePoint)
	r.GET("/points/bbox/search", h.searchBBox)
	r.GET("/points/nearest", h.nearest)
}

// ---------- regions ----------

func (h *Handler) listRegions(c *gin.Context) {
	org := currentOrg(c)
	if org == nil {
		return
	}
	regions, err := h.regions.ListRegions(org.ID)
	if err != nil {
		serverError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"regions": regions})
}

func (h *Handler) createRegion(c *gin.Context) {
	org := currentOrg(c)
	if org == nil {
		return
	}
	var in service.CreateRegionInput
	if err := c.ShouldBindJSON(&in); err != nil {
		badRequestJSON(c, "invalid JSON body: "+err.Error())
		return
	}
	rv, job, err := h.regions.CreateRegion(org.ID, in)
	if err != nil {
		serveError(c, err)
		return
	}
	c.JSON(http.StatusAccepted, gin.H{
		"region_version": rv,
		"reassign_job":   job,
		"message":        "region created as an immutable building version; it takes effect atomically when the recompute job completes",
	})
}

func (h *Handler) getRegion(c *gin.Context) {
	org := currentOrg(c)
	if org == nil {
		return
	}
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		badRequestJSON(c, "invalid region id")
		return
	}
	region, versions, err := h.regions.GetRegion(org.ID, id)
	if err != nil {
		serveError(c, err)
		return
	}
	out := gin.H{"region": region, "versions": versions}
	c.JSON(http.StatusOK, out)
}

func (h *Handler) publishVersion(c *gin.Context) {
	org := currentOrg(c)
	if org == nil {
		return
	}
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		badRequestJSON(c, "invalid region id")
		return
	}
	var in service.CreateRegionInput
	if err := c.ShouldBindJSON(&in); err != nil {
		badRequestJSON(c, "invalid JSON body: "+err.Error())
		return
	}
	rv, job, err := h.regions.PublishVersion(org.ID, id, in)
	if err != nil {
		serveError(c, err)
		return
	}
	c.JSON(http.StatusAccepted, gin.H{
		"region_version": rv,
		"reassign_job":   job,
		"message":        "new immutable version published to building; it takes effect atomically when the recompute job completes",
	})
}

func (h *Handler) getJob(c *gin.Context) {
	org := currentOrg(c)
	if org == nil {
		return
	}
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		badRequestJSON(c, "invalid job id")
		return
	}
	job, err := h.regions.GetJob(org.ID, id)
	if err != nil {
		serveError(c, err)
		return
	}
	c.JSON(http.StatusOK, job)
}

// ---------- points ----------

type batchRequest struct {
	Points []service.BatchItem `json:"points" binding:"required"`
}

func (h *Handler) batchImport(c *gin.Context) {
	org := currentOrg(c)
	if org == nil {
		return
	}
	var req batchRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		badRequestJSON(c, "invalid JSON body: "+err.Error())
		return
	}
	results, err := h.points.BatchImport(org.ID, req.Points)
	if err != nil {
		serveError(c, err)
		return
	}
	failed := 0
	for _, r := range results {
		if r.Status == "error" {
			failed++
		}
	}
	c.JSON(http.StatusOK, gin.H{
		"total":       len(results),
		"succeeded":   len(results) - failed,
		"failed":      failed,
		"row_results": results,
	})
}

type updatePointRequest struct {
	Lat             float64 `json:"lat"`
	Lng             float64 `json:"lng"`
	ExpectedVersion int64   `json:"expected_version"`
}

func (h *Handler) getPoint(c *gin.Context) {
	org := currentOrg(c)
	if org == nil {
		return
	}
	p, err := h.points.GetPoint(org.ID, c.Param("external_id"))
	if err != nil {
		serveError(c, err)
		return
	}
	c.JSON(http.StatusOK, p)
}

func (h *Handler) updatePoint(c *gin.Context) {
	org := currentOrg(c)
	if org == nil {
		return
	}
	var req updatePointRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		badRequestJSON(c, "invalid JSON body: "+err.Error())
		return
	}
	p, status, err := h.points.UpdatePoint(org.ID, c.Param("external_id"), req.Lat, req.Lng, req.ExpectedVersion)
	if err != nil {
		serveError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": status, "point": p})
}

func (h *Handler) searchBBox(c *gin.Context) {
	org := currentOrg(c)
	if org == nil {
		return
	}
	q := c.Request.URL.Query()
	minLat, e1 := parseFloat(q.Get("min_lat"))
	maxLat, e2 := parseFloat(q.Get("max_lat"))
	minLng, e3 := parseFloat(q.Get("min_lng"))
	maxLng, e4 := parseFloat(q.Get("max_lng"))
	if e1 != nil || e2 != nil || e3 != nil || e4 != nil {
		badRequestJSON(c, "min_lat,max_lat,min_lng,max_lng are required numeric query parameters")
		return
	}
	limit := 0
	if s := q.Get("limit"); s != "" {
		v, err := strconv.Atoi(s)
		if err != nil {
			badRequestJSON(c, "invalid limit")
			return
		}
		limit = v
	}
	res, err := h.points.BBox(org.ID, geometry.BBox{
		MinLat: minLat, MaxLat: maxLat, MinLng: minLng, MaxLng: maxLng,
	}, limit)
	if err != nil {
		serveError(c, err)
		return
	}
	c.JSON(http.StatusOK, res)
}

func (h *Handler) nearest(c *gin.Context) {
	org := currentOrg(c)
	if org == nil {
		return
	}
	q := c.Request.URL.Query()
	lat, e1 := parseFloat(q.Get("lat"))
	lng, e2 := parseFloat(q.Get("lng"))
	if e1 != nil || e2 != nil {
		badRequestJSON(c, "lat and lng are required numeric query parameters")
		return
	}
	n := 10
	if s := q.Get("n"); s != "" {
		v, err := strconv.Atoi(s)
		if err != nil {
			badRequestJSON(c, "invalid n")
			return
		}
		n = v
	}
	res, err := h.points.Nearest(org.ID, geometry.Vertex{Lng: lng, Lat: lat}, n)
	if err != nil {
		serveError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"count": len(res), "points": res})
}

// ---------- helpers ----------

func parseFloat(s string) (float64, error) {
	if s == "" {
		return 0, errors.New("missing")
	}
	return strconv.ParseFloat(s, 64)
}

func serveError(c *gin.Context, err error) {
	var ve *service.ValidationError
	if errors.As(err, &ve) {
		c.JSON(ve.Status, gin.H{"error": ve.Message})
		return
	}
	serverError(c, err)
}

func badRequestJSON(c *gin.Context, msg string) {
	c.JSON(http.StatusBadRequest, gin.H{"error": msg})
}

func serverError(c *gin.Context, err error) {
	c.JSON(http.StatusInternalServerError, gin.H{"error": "internal error", "detail": err.Error()})
}

// decodePolygon is exported for tests/tools that want the same JSON shape.
func decodePolygon(raw string) []geometry.Vertex {
	var v []geometry.Vertex
	_ = json.Unmarshal([]byte(raw), &v)
	return v
}

var _ = models.Point{}

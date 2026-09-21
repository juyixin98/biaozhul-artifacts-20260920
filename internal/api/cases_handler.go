package api

import (
	"net/http"

	"forensiccore/internal/auth"
	"forensiccore/internal/cases"

	"github.com/gin-gonic/gin"
)

func (s *Server) createCase(c *gin.Context) {
	var in cases.CreateInput
	if err := c.ShouldBindJSON(&in); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	in.Actor = auth.FromContext(c).Name()
	kase, ev, err := s.services.Cases.Create(c.Request.Context(), in)
	if err != nil {
		mapError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"case": kase, "genesis_event": ev})
}

func (s *Server) listCases(c *gin.Context) {
	out, err := s.services.Cases.List(c.Request.Context())
	if err != nil {
		mapError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"cases": out})
}

func (s *Server) getCase(c *gin.Context) {
	kase, err := s.services.Cases.Get(c.Request.Context(), c.Param("id"))
	if err != nil {
		mapError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"case": kase})
}

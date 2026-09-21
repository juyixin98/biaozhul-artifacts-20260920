package api

import (
	"net/http"

	"forensiccore/internal/auth"

	"github.com/gin-gonic/gin"
)

func (s *Server) startReview(c *gin.Context) {
	actor := auth.FromContext(c).Name()
	job, err := s.services.Review.Start(c.Request.Context(), c.Param("eid"), actor)
	if err != nil {
		mapError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"review": job})
}

func (s *Server) listReviews(c *gin.Context) {
	jobs, err := s.services.Review.ListByCase(c.Request.Context(), c.Param("id"))
	if err != nil {
		mapError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"reviews": jobs})
}

func (s *Server) getReview(c *gin.Context) {
	job, err := s.services.Review.Get(c.Request.Context(), c.Param("rid"))
	if err != nil {
		mapError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"review": job})
}

func (s *Server) resumeReview(c *gin.Context) {
	actor := auth.FromContext(c).Name()
	job, err := s.services.Review.Resume(c.Request.Context(), c.Param("rid"), actor)
	if err != nil {
		mapError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"review": job})
}

func (s *Server) cancelReview(c *gin.Context) {
	if err := s.services.Review.Cancel(c.Param("rid")); err != nil {
		mapError(c, err)
		return
	}
	c.JSON(http.StatusAccepted, gin.H{"status": "cancel requested"})
}

func (s *Server) listChain(c *gin.Context) {
	evs, err := s.services.Chain.ListEvents(c.Request.Context(), c.Param("id"))
	if err != nil {
		mapError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"events": evs})
}

func (s *Server) verifyChain(c *gin.Context) {
	report, err := s.services.Chain.Verify(c.Request.Context(), c.Param("id"))
	if err != nil {
		mapError(c, err)
		return
	}
	c.JSON(http.StatusOK, report)
}

func (s *Server) getReport(c *gin.Context) {
	rep, err := s.services.Report.Build(c.Request.Context(), c.Param("id"))
	if err != nil {
		mapError(c, err)
		return
	}
	c.JSON(http.StatusOK, rep)
}

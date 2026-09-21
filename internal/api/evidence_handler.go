package api

import (
	"net/http"

	"forensiccore/internal/auth"
	"forensiccore/internal/evidence"

	"github.com/gin-gonic/gin"
)

func (s *Server) registerEvidence(c *gin.Context) {
	var in evidence.RegisterInput
	if err := c.ShouldBindJSON(&in); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	in.CaseID = c.Param("id")
	in.Actor = auth.FromContext(c).Name()
	ev, chainEv, err := s.services.Evidence.Register(c.Request.Context(), in)
	if err != nil {
		mapError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"evidence": ev, "chain_event": chainEv})
}

func (s *Server) listEvidence(c *gin.Context) {
	out, err := s.services.Evidence.ListByCase(c.Request.Context(), c.Param("id"))
	if err != nil {
		mapError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"evidence": out})
}

func (s *Server) getEvidence(c *gin.Context) {
	ev, err := s.services.Evidence.Get(c.Request.Context(), c.Param("eid"))
	if err != nil {
		mapError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"evidence": ev})
}

func (s *Server) transfer(c *gin.Context) {
	var in evidence.TransferInput
	if err := c.ShouldBindJSON(&in); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	in.EvidenceID = c.Param("eid")
	in.Actor = auth.FromContext(c).Name()
	ev, err := s.services.Evidence.Transfer(c.Request.Context(), in)
	if err != nil {
		mapError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"chain_event": ev})
}

func (s *Server) addNote(c *gin.Context) {
	var in evidence.NoteInput
	if err := c.ShouldBindJSON(&in); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	in.CaseID = c.Param("id")
	in.Actor = auth.FromContext(c).Name()
	ev, err := s.services.Evidence.AddNote(c.Request.Context(), in)
	if err != nil {
		mapError(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"chain_event": ev})
}

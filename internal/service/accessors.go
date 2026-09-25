package service

import (
	"encoding/json"
	"os"

	"buildprovenance/internal/provenance"
)

// ListArtifacts returns all indexed artifacts.
func (s *Service) ListArtifacts() []provenance.Artifact { return s.artifacts.List() }

// GetArtifact returns one artifact by ID.
func (s *Service) GetArtifact(id string) (provenance.Artifact, error) { return s.artifacts.Get(id) }

// ReadArtifactBytes returns the stored bytes of an artifact.
func (s *Service) ReadArtifactBytes(id string) ([]byte, error) {
	a, err := s.artifacts.Get(id)
	if err != nil {
		return nil, err
	}
	return s.artifacts.CAS().Get(a.Digest)
}

// ListRecords returns all attestation records in log order.
func (s *Service) ListRecords() []provenance.Record { return s.log.All() }

// GetRecord returns the attestation for an artifact.
func (s *Service) GetRecord(artifactID string) (provenance.Record, error) {
	return s.log.Get(artifactID)
}

// LogTail returns the current log chain head.
func (s *Service) LogTail() string { return s.log.Tail() }

// SaveTools persists registered tool definitions to path as JSON. They are
// restored on restart so previously attested builds remain verifiable; the
// stored digest is still re-derived from files on disk during verify.
func (s *Service) SaveTools(path string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	tools := make([]provenance.Tool, 0, len(s.tools))
	for _, t := range s.tools {
		tools = append(tools, t)
	}
	b, err := json.MarshalIndent(tools, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// LoadTools restores tool definitions previously written with SaveTools.
func (s *Service) LoadTools(path string) error {
	b, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if len(b) == 0 {
		return nil
	}
	var tools []provenance.Tool
	if err := json.Unmarshal(b, &tools); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, t := range tools {
		s.tools[t.Name] = t
	}
	return nil
}

package service

import "buildprovenance/internal/provenance"

// ForgeRecordForTest replaces the in-memory attestation of artifactID with
// rec. It exists only for acceptance tests that simulate a forged proof
// (tampered content, swapped upstream hash, invented cycle). The forged
// record's own recomputed hash/HMAC can be valid — verification must still
// catch semantic inconsistencies. It is deliberately not exposed on the
// HTTP API.
func (s *Service) ForgeRecordForTest(rec provenance.Record) error {
	return s.log.ForgeRecordForTest(rec)
}

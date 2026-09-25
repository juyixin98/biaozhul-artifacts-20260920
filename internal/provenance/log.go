package provenance

import (
	"bufio"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sync"

	"buildprovenance/internal/canonical"
)

// Log is an append-only, tamper-evident attestation log backed by a JSONL
// file. Each record commits to:
//
//   - its own attestation content (canonical JSON, excluding sig),
//   - the previous record's hash (linear log chain, genesis prev = sha256 of
//     empty string),
//   - and is authenticated with an HMAC keyed by a service-local secret.
//
// The log is an in-memory index after load; Append fsyncs each new line.
type Log struct {
	path string
	key  []byte
	mu   sync.RWMutex
	recs []Record
	byID map[string]Record // artifactID -> record
	tail string            // hash of last record
}

// genesisHash is the predecessor of the first record: sha256 of empty input.
var genesisHash = func() string {
	s := sha256.Sum256(nil)
	return hex.EncodeToString(s[:])
}()

// OpenLog opens (creating if needed) the log file at path with the HMAC key.
// Existing entries are read and the log chain is validated structurally.
func OpenLog(path string, key []byte) (*Log, error) {
	if len(key) != 32 {
		return nil, fmt.Errorf("log: HMAC key must be 32 bytes, got %d", len(key))
	}
	if err := os.MkdirAll(filepathDir(path), 0o755); err != nil {
		return nil, err
	}
	l := &Log{path: path, key: key, byID: map[string]Record{}, tail: genesisHash}
	if err := l.loadLocked(); err != nil {
		return nil, err
	}
	return l, nil
}

// RecordHash computes the canonical content hash of a record. Exported so
// verification and tests can recompute it independently. Sig and RecordHash
// are excluded (RecordHash would otherwise be self-referential).
func RecordHash(r Record) (string, error) {
	return recordHashRaw(r)
}

func recordHashRaw(r Record) (string, error) {
	m := recordMap(r)
	delete(m, "sig")
	delete(m, "recordHash")
	b, err := canonical.Encode(m)
	if err != nil {
		return "", err
	}
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:]), nil
}

// recordMap converts a record to a generic map via JSON round-trip so that
// canonical encoding sees the same representation as API clients.
func recordMap(r Record) map[string]any {
	b, _ := json.Marshal(r)
	var m map[string]any
	dec := json.NewDecoder(bytesReader(b))
	dec.UseNumber()
	_ = dec.Decode(&m)
	return m
}

func (l *Log) sign(hexHash string) string {
	mac := hmac.New(sha256.New, l.key)
	mac.Write([]byte(hexHash))
	return hex.EncodeToString(mac.Sum(nil))
}

// Sign exposes the HMAC over a record hash. Verification recomputes it
// independently using the service-local key.
func (l *Log) Sign(hexHash string) string { return l.sign(hexHash) }

// Key returns the HMAC key (test support for forging records with the key
// but against a tampered content hash).
func (l *Log) Key() []byte { return append([]byte(nil), l.key...) }

// Append finalizes (recordHash, sig) for r and appends it. Prev is taken from
// the caller-supplied value and validated against the log tail so callers
// cannot fork the chain.
func (l *Log) Append(r Record) (Record, error) {
	l.mu.Lock()
	defer l.mu.Unlock()

	if r.Prev != l.tail {
		return Record{}, fmt.Errorf("log: prev mismatch (fork): got %s, expected %s", r.Prev, l.tail)
	}
	if r.ArtifactID == "" {
		return Record{}, fmt.Errorf("log: record missing artifactId")
	}
	if _, exists := l.byID[r.ArtifactID]; exists {
		return Record{}, fmt.Errorf("log: duplicate attestation for artifact %s", r.ArtifactID)
	}
	h, err := recordHashRaw(r)
	if err != nil {
		return Record{}, err
	}
	r.RecordHash = h
	r.Sig = l.sign(h)

	f, err := os.OpenFile(l.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return Record{}, err
	}
	defer f.Close()
	enc := json.NewEncoder(f)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(r); err != nil {
		return Record{}, err
	}
	if err := f.Sync(); err != nil {
		return Record{}, err
	}
	l.recs = append(l.recs, r)
	l.byID[r.ArtifactID] = r
	l.tail = h
	return r, nil
}

// Get returns the attestation for an artifact.
func (l *Log) Get(artifactID string) (Record, error) {
	l.mu.RLock()
	defer l.mu.RUnlock()
	r, ok := l.byID[artifactID]
	if !ok {
		return Record{}, fmt.Errorf("%w: record for artifact %s", ErrNotFound, artifactID)
	}
	return r, nil
}

// All returns a copy of all records in log order.
func (l *Log) All() []Record {
	l.mu.RLock()
	defer l.mu.RUnlock()
	out := make([]Record, len(l.recs))
	copy(out, l.recs)
	return out
}

// Tail returns the hash of the last appended record.
func (l *Log) Tail() string {
	l.mu.RLock()
	defer l.mu.RUnlock()
	return l.tail
}

// PrevOf returns the predecessor hash stored in the record for artifactID
// (test support for rewriting log lines).
func (l *Log) PrevOf(artifactID string) (string, error) {
	l.mu.RLock()
	defer l.mu.RUnlock()
	r, ok := l.byID[artifactID]
	if !ok {
		return "", fmt.Errorf("%w: record for artifact %s", ErrNotFound, artifactID)
	}
	return r.Prev, nil
}

// ForgeRecordForTest replaces an in-memory record and rebuilds the slice.
// Test-only: simulates an attacker who can tamper with attestation state.
func (l *Log) ForgeRecordForTest(rec Record) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if _, ok := l.byID[rec.ArtifactID]; !ok {
		return fmt.Errorf("%w: record for artifact %s", ErrNotFound, rec.ArtifactID)
	}
	l.byID[rec.ArtifactID] = rec
	for i, r := range l.recs {
		if r.ArtifactID == rec.ArtifactID {
			l.recs[i] = rec
			return nil
		}
	}
	return nil
}

// LoadResult reports structural problems found while reading the log file.
type LoadResult struct {
	Count int
	// BadEntry holds an error for the first malformed/broken line; the log
	// refuses to open in that case rather than silently truncating.
	Err error
}

func (l *Log) loadLocked() error {
	f, err := os.Open(l.path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	defer f.Close()

	prev := genesisHash
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1024*1024), 16*1024*1024)
	lineNo := 0
	for sc.Scan() {
		lineNo++
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var r Record
		if err := json.Unmarshal(line, &r); err != nil {
			return fmt.Errorf("log: line %d: malformed json: %w", lineNo, err)
		}
		h, err := recordHashRaw(r)
		if err != nil {
			return fmt.Errorf("log: line %d: %w", lineNo, err)
		}
		if r.RecordHash != h {
			return fmt.Errorf("log: line %d: record hash mismatch for artifact %s (content tampered)", lineNo, r.ArtifactID)
		}
		if !hmac.Equal(bytesHex(r.Sig), bytesHex(l.sign(h))) {
			return fmt.Errorf("log: line %d: bad HMAC signature for artifact %s", lineNo, r.ArtifactID)
		}
		if r.Prev != prev {
			return fmt.Errorf("log: line %d: broken chain link (expected prev %s, got %s)", lineNo, prev, r.Prev)
		}
		if _, dup := l.byID[r.ArtifactID]; dup {
			return fmt.Errorf("log: line %d: duplicate artifact %s", lineNo, r.ArtifactID)
		}
		l.recs = append(l.recs, r)
		l.byID[r.ArtifactID] = r
		prev = h
	}
	if err := sc.Err(); err != nil {
		return fmt.Errorf("log: read: %w", err)
	}
	l.tail = prev
	return nil
}

// VerifyChain re-reads nothing but recomputes over in-memory records. Used by
// tests after on-disk tampering + reload attempts; the open itself already
// fails on a broken file, so this covers in-memory mutation checks.
func (l *Log) VerifyChain() error {
	l.mu.RLock()
	defer l.mu.RUnlock()
	prev := genesisHash
	for i, r := range l.recs {
		h, err := recordHashRaw(r)
		if err != nil {
			return err
		}
		if r.RecordHash != h {
			return fmt.Errorf("record %d (%s): hash mismatch", i, r.ArtifactID)
		}
		wantSig := l.sign(h)
		if !hmac.Equal(bytesHex(r.Sig), bytesHex(wantSig)) {
			return fmt.Errorf("record %d (%s): bad signature", i, r.ArtifactID)
		}
		if r.Prev != prev {
			return fmt.Errorf("record %d (%s): broken link", i, r.ArtifactID)
		}
		prev = h
	}
	return nil
}

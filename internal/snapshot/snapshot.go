// Package snapshot manages on-disk full state snapshots.
//
// Directory layout under dataDir/snapshots/:
//
//	snap-H<height>-<id>/
//	  state.dat      canonical binary account table + trailing checksum
//	  manifest.json  metadata, including state file checksum and an
//	                 Ed25519 signature over the canonical manifest bytes
//	.building-H<height>-<id>/   crash leftovers (never listed as available)
//	.invalid-.../               quarantined snapshots failing verification
//
// Commit is atomic: the state file and manifest are fully written and
// fsynced inside a temp directory, then the directory rename is fsynced.
// A reader therefore never observes a snapshot without both files.
package snapshot

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/example/snapshotprune/internal/crypto"
	"github.com/example/snapshotprune/internal/state"
	"github.com/example/snapshotprune/internal/types"
)

const (
	// stateMagic prefixes every state file.
	stateMagic = "SPS1"
	// KeepCount is the number of snapshots retained by pruning.
	KeepCount = 3
	dirName   = "snapshots"
)

// ErrAlreadyBuilding is returned when a snapshot build is already running.
var ErrAlreadyBuilding = errors.New("snapshot build already in progress")

// ErrExists is returned when a usable snapshot already exists at height.
var ErrExists = errors.New("snapshot already exists at height")

// Manifest is the signed metadata file of one snapshot.
type Manifest struct {
	Height          uint64 `json:"height"`
	ID              string `json:"id"`
	ChainID         string `json:"chain_id"`
	NumAccounts     int    `json:"num_accounts"`
	StateRoot       string `json:"state_root"`
	TotalSupply     uint64 `json:"total_supply"`
	StateChecksum   string `json:"state_checksum"` // hex SHA-256 of state.dat bytes
	CreatedUnixNano int64  `json:"created_unix_nano"`
	NodePubKey      string `json:"node_pubkey"`
	Signature       string `json:"signature"` // over CanonicalManifest() of all other fields
}

// canonicalManifest is the unsigned wire form that gets signed.
type canonicalManifest struct {
	Height          uint64 `json:"height"`
	ID              string `json:"id"`
	ChainID         string `json:"chain_id"`
	NumAccounts     int    `json:"num_accounts"`
	StateRoot       string `json:"state_root"`
	TotalSupply     uint64 `json:"total_supply"`
	StateChecksum   string `json:"state_checksum"`
	CreatedUnixNano int64  `json:"created_unix_nano"`
	NodePubKey      string `json:"node_pubkey"`
}

// CanonicalBytes returns the deterministic JSON bytes that are signed.
func (m *Manifest) CanonicalBytes() ([]byte, error) {
	c := canonicalManifest{
		Height:          m.Height,
		ID:              m.ID,
		ChainID:         m.ChainID,
		NumAccounts:     m.NumAccounts,
		StateRoot:       m.StateRoot,
		TotalSupply:     m.TotalSupply,
		StateChecksum:   m.StateChecksum,
		CreatedUnixNano: m.CreatedUnixNano,
		NodePubKey:      m.NodePubKey,
	}
	return json.Marshal(c)
}

// Info is a lightweight view returned on the API.
type Info struct {
	Height          uint64 `json:"height"`
	ID              string `json:"id"`
	StateRoot       string `json:"state_root"`
	NumAccounts     int    `json:"num_accounts"`
	TotalSupply     uint64 `json:"total_supply"`
	CreatedUnixNano int64  `json:"created_unix_nano"`
	Refs            int    `json:"refs"`
}

// Manager owns the snapshot directory and its in-memory index.
type Manager struct {
	dir      string
	chainID  string
	nodeKey  *crypto.NodeKey
	mu       sync.RWMutex
	items    map[uint64]*snap
	building map[uint64]string // height -> build id (for interrupt/listing)
}

type snap struct {
	dir      string
	manifest *Manifest
	refs     int
}

// NewManager opens/creates the snapshot directory and scans for usable
// snapshots. Crash leftovers are ignored (not listed as available).
func NewManager(dataDir, chainID string, key *crypto.NodeKey) (*Manager, error) {
	m := &Manager{
		dir:      filepath.Join(dataDir, dirName),
		chainID:  chainID,
		nodeKey:  key,
		items:    map[uint64]*snap{},
		building: map[uint64]string{},
	}
	if err := os.MkdirAll(m.dir, 0o755); err != nil {
		return nil, err
	}
	if err := m.scan(); err != nil {
		return nil, err
	}
	return m, nil
}

// Dir returns the snapshots directory.
func (m *Manager) Dir() string { return m.dir }

func (m *Manager) scan() error {
	entries, err := os.ReadDir(m.dir)
	if err != nil {
		return err
	}
	for _, e := range entries {
		name := e.Name()
		if !e.IsDir() {
			continue
		}
		switch {
		case strings.HasPrefix(name, ".building-"):
			// Crash/abort leftover. Never considered available; cleanup is
			// explicit via Recover so a crash cannot resurrect a partial snap.
			continue
		case strings.HasPrefix(name, ".invalid-"):
			continue
		case !strings.HasPrefix(name, "snap-H"):
			continue
		}
		sd := filepath.Join(m.dir, name)
		man, err := m.verify(sd)
		if err != nil {
			// Missing files or any verification failure -> quarantine.
			_ = os.Rename(sd, filepath.Join(m.dir, ".invalid-"+name))
			continue
		}
		m.items[man.Height] = &snap{dir: sd, manifest: man}
	}
	return nil
}

// verify loads both files, checks checksums, Ed25519 manifest signature,
// and recomputes the state root from the decoded account table.
func (m *Manager) verify(snapDir string) (*Manifest, error) {
	statePath := filepath.Join(snapDir, "state.dat")
	manPath := filepath.Join(snapDir, "manifest.json")

	// os.Stat on state.dat: a snap dir without its state file must never be
	// listed as available.
	if fi, err := os.Stat(statePath); err != nil || fi.Size() == 0 {
		return nil, fmt.Errorf("state file missing or empty: %w", err)
	}
	if fi, err := os.Stat(manPath); err != nil || fi.Size() == 0 {
		return nil, fmt.Errorf("manifest missing or empty: %w", err)
	}
	stateBytes, err := os.ReadFile(statePath)
	if err != nil {
		return nil, err
	}
	manBytes, err := os.ReadFile(manPath)
	if err != nil {
		return nil, err
	}
	var man Manifest
	if err := json.Unmarshal(manBytes, &man); err != nil {
		return nil, fmt.Errorf("parse manifest: %w", err)
	}

	// Checksum of the state file.
	sum := sha256.Sum256(stateBytes)
	if hex.EncodeToString(sum[:]) != man.StateChecksum {
		return nil, errors.New("state file checksum mismatch")
	}

	// State file parses and internal trailing checksum matches.
	tbl, err := decodeState(stateBytes)
	if err != nil {
		return nil, err
	}

	// Manifest signature by the configured node key.
	pub, err := hex.DecodeString(man.NodePubKey)
	if err != nil {
		return nil, fmt.Errorf("manifest pubkey: %w", err)
	}
	sig, err := hex.DecodeString(man.Signature)
	if err != nil {
		return nil, fmt.Errorf("manifest signature: %w", err)
	}
	canon, err := man.CanonicalBytes()
	if err != nil {
		return nil, err
	}
	if err := crypto.VerifyManifest(pub, canon, sig); err != nil {
		return nil, err
	}
	if hex.EncodeToString(m.nodeKey.Pub) != man.NodePubKey {
		return nil, errors.New("snapshot was signed by a different node key")
	}
	if man.ChainID != m.chainID {
		return nil, fmt.Errorf("snapshot chain id %q does not match %q", man.ChainID, m.chainID)
	}
	if len(tbl) != man.NumAccounts {
		return nil, fmt.Errorf("account count mismatch: state has %d, manifest says %d", len(tbl), man.NumAccounts)
	}
	if state.Root(tbl).Hex() != man.StateRoot {
		return nil, errors.New("state root in manifest does not match decoded state")
	}
	if state.TotalSupply(tbl) != man.TotalSupply {
		return nil, errors.New("total supply in manifest does not match decoded state")
	}
	return &man, nil
}

// BeginBuild reserves a height. Commit or AbortBuild must be called with
// the returned build ID. A second concurrent begin at the same or another
// height is rejected: snapshot construction is single-flight.
func (m *Manager) BeginBuild(height uint64) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if len(m.building) != 0 {
		return "", ErrAlreadyBuilding
	}
	if _, ok := m.items[height]; ok {
		return "", fmt.Errorf("%w: height %d", ErrExists, height)
	}
	id := randomID()
	m.building[height] = id
	return id, nil
}

// Commit materializes the snapshot for a reserved build and atomically
// publishes it. failHook is for tests and is normally nil.
func (m *Manager) Commit(height uint64, buildID string, tbl state.Table, failHook func() error) error {
	m.mu.RLock()
	wantID, ok := m.building[height]
	m.mu.RUnlock()
	if !ok || wantID != buildID {
		return errors.New("no matching in-progress build")
	}

	root := state.Root(tbl)
	stateBytes, err := encodeState(tbl)
	if err != nil {
		return err
	}
	sum := sha256.Sum256(stateBytes)

	man := &Manifest{
		Height:          height,
		ID:              buildID,
		ChainID:         m.chainID,
		NumAccounts:     len(tbl),
		StateRoot:       root.Hex(),
		TotalSupply:     state.TotalSupply(tbl),
		StateChecksum:   hex.EncodeToString(sum[:]),
		CreatedUnixNano: time.Now().UnixNano(),
		NodePubKey:      hex.EncodeToString(m.nodeKey.Pub),
	}
	canon, err := man.CanonicalBytes()
	if err != nil {
		return err
	}
	man.Signature = hex.EncodeToString(m.nodeKey.SignManifest(canon))

	tmp := filepath.Join(m.dir, fmt.Sprintf(".building-H%d-%s", height, buildID))
	final := filepath.Join(m.dir, fmt.Sprintf("snap-H%d-%s", height, buildID))
	if err := os.MkdirAll(tmp, 0o755); err != nil {
		return err
	}
	// Write state file first (write-to-temp + rename inside the build dir).
	if err := writeFileAtomic(filepath.Join(tmp, "state.dat"), stateBytes); err != nil {
		return err
	}
	if failHook != nil {
		if err := failHook(); err != nil {
			return err
		}
	}
	manBytes, err := json.MarshalIndent(man, "", "  ")
	if err != nil {
		return err
	}
	if err := writeFileAtomic(filepath.Join(tmp, "manifest.json"), manBytes); err != nil {
		return err
	}
	if err := crypto.FsyncDir(tmp); err != nil {
		return err
	}
	// Atomic publish: directory rename.
	if err := os.Rename(tmp, final); err != nil {
		return err
	}
	if err := crypto.FsyncDir(m.dir); err != nil {
		return err
	}

	m.mu.Lock()
	delete(m.building, height)
	m.items[height] = &snap{dir: final, manifest: man}
	m.mu.Unlock()
	return nil
}

// AbortBuild releases a build reservation and removes its temp directory.
func (m *Manager) AbortBuild(height uint64, buildID string) error {
	m.mu.Lock()
	wantID, ok := m.building[height]
	if ok && wantID == buildID {
		delete(m.building, height)
	}
	m.mu.Unlock()
	tmp := filepath.Join(m.dir, fmt.Sprintf(".building-H%d-%s", height, buildID))
	if err := os.RemoveAll(tmp); err != nil {
		return err
	}
	return crypto.FsyncDir(m.dir)
}

// Building reports the currently reserved build height, if any.
func (m *Manager) Building() (uint64, string, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	for h, id := range m.building {
		return h, id, true
	}
	return 0, "", false
}

// Acquire pins the snapshot at height for the duration of a read. Release
// must be called. Pins make pruning refuse to delete the snapshot.
func (m *Manager) Acquire(height uint64) (func(), error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	s, ok := m.items[height]
	if !ok {
		return nil, fmt.Errorf("no snapshot at height %d", height)
	}
	s.refs++
	return func() {
		m.mu.Lock()
		s.refs--
		m.mu.Unlock()
	}, nil
}

// Load reads and decodes the state table of a snapshot.
func (m *Manager) Load(height uint64) (state.Table, error) {
	m.mu.RLock()
	s, ok := m.items[height]
	m.mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("no snapshot at height %d", height)
	}
	b, err := os.ReadFile(filepath.Join(s.dir, "state.dat"))
	if err != nil {
		return nil, err
	}
	return decodeState(b)
}

// Has reports whether a usable snapshot exists at height.
func (m *Manager) Has(height uint64) bool {
	m.mu.RLock()
	defer m.mu.RUnlock()
	_, ok := m.items[height]
	return ok
}

// List returns all usable snapshots newest-first.
func (m *Manager) List() []Info {
	m.mu.RLock()
	defer m.mu.RUnlock()
	out := make([]Info, 0, len(m.items))
	for _, s := range m.items {
		mn := s.manifest
		out = append(out, Info{
			Height:          mn.Height,
			ID:              mn.ID,
			StateRoot:       mn.StateRoot,
			NumAccounts:     mn.NumAccounts,
			TotalSupply:     mn.TotalSupply,
			CreatedUnixNano: mn.CreatedUnixNano,
			Refs:            s.refs,
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Height > out[j].Height })
	return out
}

// HeightsNewestFirst returns usable snapshot heights newest-first.
func (m *Manager) HeightsNewestFirst() []uint64 {
	m.mu.RLock()
	defer m.mu.RUnlock()
	out := make([]uint64, 0, len(m.items))
	for h := range m.items {
		out = append(out, h)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] > out[j] })
	return out
}

// LatestAtOrBelow returns the highest snapshot height <= h, or false.
func (m *Manager) LatestAtOrBelow(h uint64) (uint64, bool) {
	var best uint64
	found := false
	for _, sh := range m.HeightsNewestFirst() {
		if sh <= h && (!found || sh > best) {
			best = sh
			found = true
		}
	}
	return best, found
}

// Refs returns the current pin count of a snapshot.
func (m *Manager) Refs(height uint64) int {
	m.mu.RLock()
	defer m.mu.RUnlock()
	if s, ok := m.items[height]; ok {
		return s.refs
	}
	return 0
}

// deleteLocked removes a snapshot with no outstanding pins.
func (m *Manager) deleteLocked(s *snap) error {
	if s.refs != 0 {
		return fmt.Errorf("snapshot at height %d has %d active readers", s.manifest.Height, s.refs)
	}
	if err := os.RemoveAll(s.dir); err != nil {
		return err
	}
	delete(m.items, s.manifest.Height)
	return crypto.FsyncDir(m.dir)
}

// Delete removes one snapshot, refusing while it has pins.
func (m *Manager) Delete(height uint64) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	s, ok := m.items[height]
	if !ok {
		return fmt.Errorf("no snapshot at height %d", height)
	}
	return m.deleteLocked(s)
}

// PruneResult describes a retention prune.
type PruneResult struct {
	KeptHeights      []uint64 `json:"kept_heights"`
	DeletedHeights   []uint64 `json:"deleted_heights"`
	ProtectedHeights []uint64 `json:"protected_by_readers"`
}

// PruneOld keeps at most KeepCount snapshots. Older snapshots beyond that
// are deleted unless they currently have reader pins (those are reported as
// protected, never deleted out from under active readers).
func (m *Manager) PruneOld() (*PruneResult, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	heights := make([]uint64, 0, len(m.items))
	for h := range m.items {
		heights = append(heights, h)
	}
	sort.Slice(heights, func(i, j int) bool { return heights[i] > heights[j] })

	res := &PruneResult{
		KeptHeights:      []uint64{},
		DeletedHeights:   []uint64{},
		ProtectedHeights: []uint64{},
	}
	for i, h := range heights {
		if i < KeepCount {
			res.KeptHeights = append(res.KeptHeights, h)
			continue
		}
		s := m.items[h]
		if s.refs != 0 {
			res.ProtectedHeights = append(res.ProtectedHeights, h)
			continue
		}
		if err := m.deleteLocked(s); err != nil {
			return nil, err
		}
		res.DeletedHeights = append(res.DeletedHeights, h)
	}
	// Protected old snapshots still count as present; kept list should list
	// everything that survived so callers can reason about the floor.
	res.KeptHeights = append(res.KeptHeights, res.ProtectedHeights...)
	sort.Slice(res.KeptHeights, func(i, j int) bool { return res.KeptHeights[i] > res.KeptHeights[j] })
	return res, nil
}

// OldestKept returns the smallest height among usable snapshots.
func (m *Manager) OldestKept() (uint64, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	oldest := uint64(0)
	found := false
	for h := range m.items {
		if !found || h < oldest {
			oldest = h
			found = true
		}
	}
	return oldest, found
}

// Recover removes leftover .building directories left by a crash or an
// interrupted build, and returns their names. These were never usable.
func (m *Manager) Recover() ([]string, error) {
	entries, err := os.ReadDir(m.dir)
	if err != nil {
		return nil, err
	}
	var removed []string = []string{}
	for _, e := range entries {
		if e.IsDir() && strings.HasPrefix(e.Name(), ".building-") {
			if err := os.RemoveAll(filepath.Join(m.dir, e.Name())); err != nil {
				return removed, err
			}
			removed = append(removed, e.Name())
		}
	}
	if len(removed) > 0 {
		if err := crypto.FsyncDir(m.dir); err != nil {
			return removed, err
		}
	}
	m.mu.Lock()
	m.building = map[uint64]string{}
	m.mu.Unlock()
	return removed, nil
}

// Leftovers returns names of crash-leftover temp directories.
func (m *Manager) Leftovers() ([]string, error) {
	entries, err := os.ReadDir(m.dir)
	if err != nil {
		return nil, err
	}
	var out []string = []string{}
	for _, e := range entries {
		if e.IsDir() && strings.HasPrefix(e.Name(), ".building-") {
			out = append(out, e.Name())
		}
	}
	return out, nil
}

// ---- state file binary encoding ----
//
// Layout:
//
//	"SPL1"(4) || count(8) ||
//	  repeat count: address(20) || nonce(8) || balance(8)
//	|| checksum(32) = SHA-256(all preceding bytes)

func encodeState(tbl state.Table) ([]byte, error) {
	addrs := make([]types.Address, 0, len(tbl))
	for a := range tbl {
		addrs = append(addrs, a)
	}
	sort.Slice(addrs, func(i, j int) bool {
		return addrs[i].Hex() < addrs[j].Hex()
	})
	buf := make([]byte, 0, 12+len(addrs)*36+32)
	buf = append(buf, stateMagic...)
	var n [8]byte
	binary.BigEndian.PutUint64(n[:], uint64(len(addrs)))
	buf = append(buf, n[:]...)
	for _, a := range addrs {
		acc := tbl[a]
		row := make([]byte, 36)
		copy(row[0:20], a[:])
		binary.BigEndian.PutUint64(row[20:28], acc.Nonce)
		binary.BigEndian.PutUint64(row[28:36], acc.Balance)
		buf = append(buf, row...)
	}
	sum := sha256.Sum256(buf)
	buf = append(buf, sum[:]...)
	return buf, nil
}

func decodeState(b []byte) (state.Table, error) {
	if len(b) < 4+8+32 {
		return nil, errors.New("state file too short")
	}
	if string(b[:4]) != stateMagic {
		return nil, errors.New("state file bad magic")
	}
	body := b[:len(b)-32]
	got := sha256.Sum256(body)
	var want [32]byte
	copy(want[:], b[len(b)-32:])
	if got != want {
		return nil, errors.New("state file internal checksum mismatch")
	}
	cnt := binary.BigEndian.Uint64(body[4:12])
	rows := body[12:]
	if uint64(len(rows)) != cnt*36 {
		return nil, fmt.Errorf("state file row length mismatch: %d rows need %d bytes, have %d",
			cnt, cnt*36, len(rows))
	}
	tbl := state.Table{}
	for i := uint64(0); i < cnt; i++ {
		row := rows[i*36 : (i+1)*36]
		var a types.Address
		copy(a[:], row[0:20])
		tbl[a] = &types.Account{
			Nonce:   binary.BigEndian.Uint64(row[20:28]),
			Balance: binary.BigEndian.Uint64(row[28:36]),
		}
	}
	return tbl, nil
}

func writeFileAtomic(path string, data []byte) error {
	f, err := os.CreateTemp(filepath.Dir(path), ".tmp-*")
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer os.Remove(tmp)
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func randomID() string {
	var b [8]byte
	if _, err := readRand(b[:]); err != nil {
		// crypto/rand failure is unrecoverable in practice.
		panic(err)
	}
	return hex.EncodeToString(b[:])
}

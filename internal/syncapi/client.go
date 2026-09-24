package syncapi

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sync"

	"merklesync/internal/store"
)

// ErrConflict is returned by a revision-checked call when the peer's tree root
// moved during the scan. The synchronizer treats it as "restart the round".
type ErrConflict struct {
	CurrentRevision int64
	CurrentEpoch    int64
}

func (e *ErrConflict) Error() string {
	return fmt.Sprintf("peer revision changed during scan (now rev=%d epoch=%d); retry required", e.CurrentRevision, e.CurrentEpoch)
}

// Stats accumulates wire-level exchange counters for one sync session.
// "sent" means bytes uploaded to the peer (/apply); "received" means bytes
// downloaded from the peer.
type Stats struct {
	RootFetches    int `json:"rootFetches"`
	NodeRequests   int `json:"nodeRequests"`
	NodeHashesRecv int `json:"nodeHashesReceived"`
	LeafRequests   int `json:"leafRequests"`
	LeafMetasRecv  int `json:"leafMetasReceived"`
	ValueRequests  int `json:"valueRequests"`
	ValuesRecv     int `json:"valuesReceived"`
	AppliedSent    int `json:"appliedEntriesSent"`
	ApplyRequests  int `json:"applyRequests"`
	BytesSent      int `json:"bytesSent"`
	BytesReceived  int `json:"bytesReceived"`
}

// Client is a minimal JSON/HTTP client for one peer replica.
type Client struct {
	base  string
	http  *http.Client
	mu    sync.Mutex
	stats Stats
}

func NewClient(baseURL string) *Client {
	return &Client{base: baseURL, http: http.DefaultClient}
}

func (c *Client) add(fn func(*Stats)) {
	c.mu.Lock()
	fn(&c.stats)
	c.mu.Unlock()
}

// Stats returns a copy of the accumulated counters.
func (c *Client) Stats() Stats {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.stats
}

func (c *Client) do(ctx context.Context, method, path string, body any, out any) error {
	var rdr io.Reader
	var bodyLen int
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rdr = bytes.NewReader(raw)
		bodyLen = len(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, rdr)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	rawResp, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	c.add(func(s *Stats) {
		s.BytesSent += bodyLen
		s.BytesReceived += len(rawResp)
	})
	if resp.StatusCode == http.StatusConflict {
		var ce conflictError
		_ = json.Unmarshal(rawResp, &ce)
		return &ErrConflict{CurrentRevision: ce.CurrentRevision, CurrentEpoch: ce.CurrentEpoch}
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("peer returned %d: %s", resp.StatusCode, string(rawResp))
	}
	if out != nil {
		if err := json.Unmarshal(rawResp, out); err != nil {
			return fmt.Errorf("decoding %s response: %w", path, err)
		}
	}
	return nil
}

func (c *Client) Root(ctx context.Context) (RootInfo, error) {
	var info RootInfo
	err := c.do(ctx, http.MethodGet, "/root", nil, &info)
	if err == nil {
		c.add(func(s *Stats) { s.RootFetches++ })
	}
	return info, err
}

func (c *Client) Nodes(ctx context.Context, revision int64, level int, indexes []int) ([]string, error) {
	var resp nodesResponse
	err := c.do(ctx, http.MethodPost, "/nodes", nodesRequest{Revision: revision, Level: level, Indexes: indexes}, &resp)
	if err == nil {
		c.add(func(s *Stats) {
			s.NodeRequests++
			s.NodeHashesRecv += len(indexes)
		})
	}
	return resp.Hashes, err
}

func (c *Client) Leaves(ctx context.Context, revision int64, buckets []int) ([]EntryMeta, error) {
	var resp leavesResponse
	err := c.do(ctx, http.MethodPost, "/leaves", leavesRequest{Revision: revision, Buckets: buckets}, &resp)
	if err == nil {
		c.add(func(s *Stats) {
			s.LeafRequests++
			s.LeafMetasRecv += len(resp.Entries)
		})
	}
	return resp.Entries, err
}

func (c *Client) Entries(ctx context.Context, revision int64, keys []string) ([]store.Entry, error) {
	var resp entriesResponse
	err := c.do(ctx, http.MethodPost, "/entries", entriesRequest{Revision: revision, Keys: keys}, &resp)
	if err == nil {
		c.add(func(s *Stats) {
			s.ValueRequests++
			s.ValuesRecv += len(resp.Entries)
		})
	}
	return resp.Entries, err
}

// Apply pushes a batch of entries to the peer. May be a subset of the batch;
// the peer returns how many actually won the LWW comparison.
func (c *Client) Apply(ctx context.Context, entries []store.Entry) (int, error) {
	var resp applyResponse
	err := c.do(ctx, http.MethodPost, "/apply", applyRequest{Entries: entries}, &resp)
	if err == nil {
		c.add(func(s *Stats) {
			s.ApplyRequests++
			s.AppliedSent += len(entries)
		})
	}
	return resp.Applied, err
}

// Put / Delete / Reset / ArmChaos / Snapshot are operational helpers used by
// the CLI demo and tests, not by the synchronizer itself.

func (c *Client) Put(ctx context.Context, key, value string, version int64) (bool, error) {
	var resp map[string]bool
	err := c.do(ctx, http.MethodPost, "/put", writeRequest{Key: key, Value: value, Version: version}, &resp)
	return resp["changed"], err
}

func (c *Client) DeleteKey(ctx context.Context, key string, version int64) (bool, error) {
	var resp map[string]bool
	err := c.do(ctx, http.MethodPost, "/delete", writeRequest{Key: key, Version: version}, &resp)
	return resp["changed"], err
}

func (c *Client) Reset(ctx context.Context) error {
	return c.do(ctx, http.MethodPost, "/reset", struct{}{}, nil)
}

type chaosArmReq = chaosRequest

func (c *Client) ArmChaos(ctx context.Context, after int, op, key, value string, version int64) error {
	return c.armChaos(ctx, after, op, key, value, version, 0)
}

// ArmChaosRepeating arms a scan-time mutation that re-arms for repeat more
// rounds after the first firing.
func (c *Client) ArmChaosRepeating(ctx context.Context, after int, op, key, value string, version, repeat int64) error {
	return c.armChaos(ctx, after, op, key, value, version, int(repeat))
}

func (c *Client) armChaos(ctx context.Context, after int, op, key, value string, version int64, repeat int) error {
	req := chaosArmReq{After: after, Op: op, Key: key, Value: value, Version: version, Repeat: repeat}
	return c.do(ctx, http.MethodPost, "/debug/chaos", req, nil)
}

func (c *Client) Snapshot(ctx context.Context) (store.Snapshot, error) {
	var snap store.Snapshot
	err := c.do(ctx, http.MethodGet, "/snapshot", nil, &snap)
	return snap, err
}

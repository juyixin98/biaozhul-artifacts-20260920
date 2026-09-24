package gc

import (
	"context"
	"fmt"
	"sort"
	"time"

	"layerregistry/internal/store"
)

// OrphanResult is the auditable outcome of an orphan-temp sweep. Orphan
// staging files are handled separately from layer GC: they are never
// content-addressable and are deleted purely by upload-session age.
type OrphanResult struct {
	Cutoff            time.Time `json:"cutoff"`
	ExpiredSessions   []string  `json:"expired_sessions"`
	UnlinkedTempFiles []string  `json:"unlinked_temp_files"`
	DeletedFiles      []string  `json:"deleted_files"`
}

// CleanupOrphans removes:
//   - temp files belonging to upload sessions older than cutoff (and the rows)
//   - temp files with no upload row at all (crash between file create and
//     insert, or leftover from a dead process)
//
// A file belonging to a live, recent session is always left alone.
func (c *Collector) CleanupOrphans(ctx context.Context, cutoff time.Time) (*OrphanResult, error) {
	res := &OrphanResult{Cutoff: cutoff}

	all, err := c.st.ListAllUploads(ctx)
	if err != nil {
		return nil, err
	}
	liveNames := map[string]bool{}
	var expired []store.ExpiredUpload
	for _, u := range all {
		if u.StartedAt.Before(cutoff) {
			expired = append(expired, u)
			res.ExpiredSessions = append(res.ExpiredSessions, u.ID)
		} else {
			liveNames[u.Name] = true
		}
	}

	for _, u := range expired {
		if err := c.fs.RemoveTemp(u.Name); err != nil {
			return nil, fmt.Errorf("remove temp %s: %w", u.Name, err)
		}
		if err := c.st.DeleteUpload(ctx, u.ID); err != nil {
			return nil, err
		}
		res.DeletedFiles = append(res.DeletedFiles, u.Name)
		_ = c.st.AuditEvent(ctx, "", "gc.orphan_session", "", "", u.Name,
			fmt.Sprintf("upload %s started %s older than cutoff", u.ID, u.StartedAt.Format(time.RFC3339)))
	}

	// Sweep temp files that no session row points at.
	if lister, ok := c.fs.(tempLister); ok {
		files, err := lister.ListTemp()
		if err != nil {
			return nil, err
		}
		sort.Strings(files)
		for _, name := range files {
			if liveNames[name] {
				continue // active, recent upload
			}
			if amongUploads(name, expired) {
				continue // just removed above
			}
			res.UnlinkedTempFiles = append(res.UnlinkedTempFiles, name)
			if err := c.fs.RemoveTemp(name); err != nil {
				return nil, err
			}
			res.DeletedFiles = append(res.DeletedFiles, name)
			_ = c.st.AuditEvent(ctx, "", "gc.orphan_file", "", "", name,
				"temp file with no upload session row")
		}
	}
	return res, nil
}

func amongUploads(name string, list []store.ExpiredUpload) bool {
	for _, u := range list {
		if u.Name == name {
			return true
		}
	}
	return false
}

type tempLister interface {
	ListTemp() ([]string, error)
}

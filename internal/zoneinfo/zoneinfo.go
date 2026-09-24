// Package zoneinfo loads time zones exclusively from the compiled IANA
// database embedded under ./files. Pinning the database version makes DST
// behavior (spring-forward gaps, fall-back overlaps) identical on every host
// that runs or tests this program, regardless of the host's own tzdata.
package zoneinfo

import (
	"embed"
	"errors"
	"fmt"
	"io/fs"
	"sync"
	"time"
)

// TZDataVersion is the pinned IANA tzdata release. Keep in sync with
// scripts/build-zoneinfo.sh.
const TZDataVersion = "2024a"

//go:embed all:files
var embedded embed.FS

// loader is the single time.Location source used by the whole program.
type loader struct {
	mu    sync.Mutex
	cache map[string]*time.Location
}

var defaultLoader = &loader{cache: map[string]*time.Location{}}

var errZoneNotFound = errors.New("time zone not found in embedded tzdata")

// Load returns the time.Location for an IANA zone name (e.g.
// "America/New_York"), resolving links/aliases. Results are cached.
func Load(name string) (*time.Location, error) {
	return defaultLoader.load(name)
}

func (l *loader) load(name string) (*time.Location, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if loc, ok := l.cache[name]; ok {
		return loc, nil
	}

	// The compiled tree stores links as regular files pointing at the target
	// (zic materializes them); Go's time.LoadLocationFromTZData handles both.
	b, err := fs.ReadFile(embedded, "files/"+name)
	if err != nil {
		return nil, fmt.Errorf("%w: %s", errZoneNotFound, name)
	}
	loc, err := time.LoadLocationFromTZData(name, b)
	if err != nil {
		return nil, fmt.Errorf("load zone %q: %w", name, err)
	}
	l.cache[name] = loc
	return loc, nil
}

// Names returns all zone names present in the embedded database, sorted.
func Names() ([]string, error) {
	var names []string
	err := fs.WalkDir(embedded, "files", func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || d.Name() == "MANIFEST" {
			return nil
		}
		names = append(names, path[len("files/"):])
		return nil
	})
	if err != nil {
		return nil, err
	}
	return names, nil
}

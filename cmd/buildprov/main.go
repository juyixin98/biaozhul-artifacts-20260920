// Command buildprov is the local build provenance service. It stores source
// inputs and build artifacts in separate content-addressed directories, runs
// declared build actions as restricted bash fixtures in throwaway work
// directories, and serves a JSON API over 127.0.0.1.
package main

import (
	"crypto/rand"
	"encoding/hex"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"buildprovenance/internal/executor"
	"buildprovenance/internal/httpapi"
	"buildprovenance/internal/provenance"
	"buildprovenance/internal/service"
)

func main() {
	var (
		addr        = flag.String("addr", "127.0.0.1:8080", "listen address (loopback by default)")
		dataDir     = flag.String("data-dir", "./.bpdata", "directory for caches, attestation log and indexes")
		workDir     = flag.String("work-dir", "", "scratch directory for isolated builds (default <data-dir>/work; kept separate from caches)")
		fixtureDir  = flag.String("fixtures", "./fixtures", "directory containing the only scripts actions may execute")
		interpreter = flag.String("interpreter", executor.DefaultInterpreter, "interpreter binary allowed to run fixture scripts")
	)
	flag.Parse()

	if err := run(*addr, *dataDir, *workDir, *fixtureDir, *interpreter); err != nil {
		log.Fatalf("buildprov: %v", err)
	}
}

func run(addr, dataDir, workDir, fixtureDir, interpreter string) error {
	if workDir == "" {
		workDir = filepath.Join(dataDir, "work")
	}
	sourceDir := filepath.Join(dataDir, "sources")
	artifactDir := filepath.Join(dataDir, "artifacts")
	logPath := filepath.Join(dataDir, "attestations.log")
	indexDir := filepath.Join(dataDir, "index")
	for _, d := range []string{dataDir, sourceDir, artifactDir, workDir, indexDir} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return err
		}
	}
	// Defensive check: cache roots and work root must be distinct.
	abs := func(p string) string { a, _ := filepath.Abs(p); return a }
	roots := []string{abs(sourceDir), abs(artifactDir), abs(workDir)}
	for i := 0; i < len(roots); i++ {
		for j := i + 1; j < len(roots); j++ {
			if roots[i] == roots[j] {
				return fmt.Errorf("source cache, artifact cache and work dir must be distinct, got duplicate %s", roots[i])
			}
		}
	}

	key, err := loadOrCreateKey(filepath.Join(dataDir, "hmac.key"))
	if err != nil {
		return err
	}

	sourceCAS, err := provenance.NewCAS(sourceDir)
	if err != nil {
		return err
	}
	artifactCAS, err := provenance.NewCAS(artifactDir)
	if err != nil {
		return err
	}
	sources := provenance.NewSourceRegistry(sourceCAS)
	artifacts := provenance.NewArtifactRegistry(artifactCAS)
	if err := sources.Load(filepath.Join(indexDir, "sources.json")); err != nil {
		return fmt.Errorf("load source index: %w", err)
	}
	if err := artifacts.Load(filepath.Join(indexDir, "artifacts.json")); err != nil {
		return fmt.Errorf("load artifact index: %w", err)
	}
	attLog, err := provenance.OpenLog(logPath, key)
	if err != nil {
		return err
	}

	policy, err := executor.NewPolicy(interpreter, fixtureDir)
	if err != nil {
		return err
	}
	svc, err := service.New(service.Config{
		Policy:    policy,
		Sources:   sources,
		Artifacts: artifacts,
		Log:       attLog,
		WorkRoot:  workDir,
	})
	if err != nil {
		return err
	}
	if err := svc.LoadTools(filepath.Join(indexDir, "tools.json")); err != nil {
		return fmt.Errorf("load tool index: %w", err)
	}
	svc.SetOnChange(func() {
		if err := saveIndexes(sources, artifacts, svc, indexDir); err != nil {
			log.Printf("persist indexes: %v", err)
		}
	})

	// Persist lookup indexes periodically and on exit (best-effort; the
	// attestation log remains the source of truth).
	stop := make(chan struct{})
	go func() {
		ticker := time.NewTicker(10 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				_ = saveIndexes(sources, artifacts, svc, indexDir)
			case <-stop:
				_ = saveIndexes(sources, artifacts, svc, indexDir)
				return
			}
		}
	}()
	defer close(stop)

	srv := &http.Server{
		Addr:              addr,
		Handler:           httpapi.NewServer(svc).Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("build provenance service listening on %s", addr)
	log.Printf("  sources cache:  %s", roots[0])
	log.Printf("  artifact cache: %s", roots[1])
	log.Printf("  work root:      %s", abs(workDir))
	log.Printf("  fixtures:       %s", fixtureDir)
	log.Printf("  attestations:   %s", logPath)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		return err
	}
	return nil
}

func saveIndexes(sources *provenance.SourceRegistry, artifacts *provenance.ArtifactRegistry, svc *service.Service, dir string) error {
	if err := sources.Save(filepath.Join(dir, "sources.json")); err != nil {
		return err
	}
	if err := artifacts.Save(filepath.Join(dir, "artifacts.json")); err != nil {
		return err
	}
	return svc.SaveTools(filepath.Join(dir, "tools.json"))
}

func loadOrCreateKey(path string) ([]byte, error) {
	if b, err := os.ReadFile(path); err == nil {
		key, err := hex.DecodeString(string(b))
		if err != nil || len(key) != 32 {
			return nil, fmt.Errorf("HMAC key file %s is corrupt", path)
		}
		return key, nil
	} else if !os.IsNotExist(err) {
		return nil, err
	}
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return nil, err
	}
	if err := os.WriteFile(path, []byte(hex.EncodeToString(key)), 0o600); err != nil {
		return nil, err
	}
	log.Printf("generated new service HMAC key at %s (0600)", path)
	return key, nil
}

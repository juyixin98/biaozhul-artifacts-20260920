// ForensicCore 本地证据处理后端入口。
package main

import (
	"log"
	"os"
	"strconv"

	"forensiccore/internal/api"
	"forensiccore/internal/chain"
	"forensiccore/internal/db"
	"forensiccore/internal/evidence"
	"forensiccore/internal/safeopen"
)

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	addr := env("FORENSIC_ADDR", ":8080")
	dsn := env("FORENSIC_MYSQL_DSN", "root:forensic@tcp(127.0.0.1:3306)/forensic?parseTime=true&charset=utf8mb4&loc=UTC")
	rootDir := env("FORENSIC_EVIDENCE_ROOT", "./samples")
	chunkSize, _ := strconv.ParseInt(env("FORENSIC_CHUNK_SIZE", "4194304"), 10, 64)

	root, err := safeopen.NewRoot(rootDir)
	if err != nil {
		log.Fatalf("evidence root: %v", err)
	}
	gormDB, err := db.OpenMySQL(dsn)
	if err != nil {
		log.Fatalf("mysql: %v", err)
	}
	if err := db.Migrate(gormDB); err != nil {
		log.Fatalf("migrate: %v", err)
	}

	chainStore := chain.NewStore(gormDB)
	svc := evidence.NewService(gormDB, root, chainStore, chunkSize)
	// 崩溃恢复：把中断时仍处于 running 的作业标记为可恢复的 paused。
	if err := svc.RecoverInterruptedJobs(); err != nil {
		log.Fatalf("recover jobs: %v", err)
	}

	log.Printf("ForensicCore listening on %s, evidence root %s", addr, root.Path())
	if err := api.NewRouter(gormDB, svc, chainStore).Run(addr); err != nil {
		log.Fatal(err)
	}
}

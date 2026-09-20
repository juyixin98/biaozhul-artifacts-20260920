package main

import (
	"log"
	"time"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"

	"proofcycle/internal/config"
	"proofcycle/internal/handler"
	"proofcycle/internal/migrate"
	"proofcycle/internal/service"
	"proofcycle/internal/storage"
)

func main() {
	cfg := config.Load()

	db, err := connectDB(cfg.DSN)
	if err != nil {
		log.Fatalf("connect database: %v", err)
	}
	if err := migrate.Run(db); err != nil {
		log.Fatalf("run migrations: %v", err)
	}
	if cfg.SeedDemo {
		if err := migrate.SeedDemo(db); err != nil {
			log.Fatalf("seed demo data: %v", err)
		}
		log.Println("demo users seeded (alice/pm, bob/designer, carol-judy/reviewers)")
	}

	st, err := storage.New(cfg.StorageRoot, cfg.MaxUploadMB*1024*1024)
	if err != nil {
		log.Fatalf("init storage: %v", err)
	}
	svc := service.New(db, st)
	r := handler.NewRouter(db, svc)

	log.Printf("proofcycle listening on %s (storage: %s, max upload: %dMB)", cfg.Addr, st.Root, cfg.MaxUploadMB)
	if err := r.Run(cfg.Addr); err != nil {
		log.Fatal(err)
	}
}

// connectDB 带重试地连接 MySQL，便于 docker-compose 中等待数据库就绪。
func connectDB(dsn string) (*gorm.DB, error) {
	var lastErr error
	for i := 0; i < 30; i++ {
		db, err := gorm.Open(mysql.Open(dsn), &gorm.Config{})
		if err == nil {
			if sqlDB, derr := db.DB(); derr == nil {
				if perr := sqlDB.Ping(); perr == nil {
					return db, nil
				} else {
					lastErr = perr
				}
			} else {
				lastErr = derr
			}
		} else {
			lastErr = err
		}
		log.Printf("waiting for database (%v)", lastErr)
		time.Sleep(time.Second)
	}
	return nil, lastErr
}

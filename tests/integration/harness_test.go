// Package integration contains end-to-end tests that exercise the real MySQL
// schema, the services and the worker pipeline. They require a reachable
// MySQL; GEOTEST_DSN overrides the default docker-compose endpoint. If no
// database is reachable the package skips, so `go test ./...` without
// infrastructure still passes.
package integration

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"testing"
	"time"

	"geoterritory/internal/geometry"
	"geoterritory/internal/jobs"
	"geoterritory/internal/models"
	"geoterritory/internal/service"
	"geoterritory/internal/store"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"
)

type env struct {
	db      *gorm.DB
	sqlDB   *sql.DB
	regions *service.RegionService
	points  *service.PointService
	runner  *jobs.Runner
	orgs    map[string]int64 // label -> id
}

var testEnv *env

func TestMain(m *testing.M) {
	dsn := os.Getenv("GEOTEST_DSN")
	if dsn == "" {
		dsn = "geoterritory:geoterritory@tcp(127.0.0.1:3306)/geoterritory?charset=utf8mb4&parseTime=true&loc=UTC&multiStatements=true"
	}
	probe, err := sql.Open("mysql", dsn)
	if err != nil || probe.Ping() != nil {
		fmt.Println("integration: MySQL not reachable, skipping")
		os.Exit(0)
	}
	_ = probe.Close()

	gdb, err := gorm.Open(mysql.Open(dsn), &gorm.Config{Logger: gormlogger.Default.LogMode(gormlogger.Silent)})
	if err != nil {
		panic(err)
	}
	sqlDB, _ := gdb.DB()

	for _, t := range []string{"assignment_staging", "reassign_jobs", "points", "region_versions", "regions", "organizations", "schema_migrations"} {
		gdb.Exec("DROP TABLE IF EXISTS " + t)
	}
	if err := store.NewMigrator(sqlDB).Up(); err != nil {
		panic(err)
	}

	testEnv = &env{
		db:      gdb,
		sqlDB:   sqlDB,
		regions: service.NewRegionService(gdb),
		points:  service.NewPointService(gdb),
		runner:  jobs.NewRunner(gdb),
		orgs:    make(map[string]int64),
	}
	for _, label := range []string{"acme", "beta", "gamma"} {
		o := models.Organization{Name: label, APIKey: "key-" + label + "-" + fmt.Sprint(time.Now().UnixNano())}
		if err := gdb.Create(&o).Error; err != nil {
			panic(err)
		}
		testEnv.orgs[label] = o.ID
	}

	os.Exit(m.Run())
}

// drainJobs runs the worker until no pending/running jobs remain.
func (e *env) drainJobs(t *testing.T) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	go e.runner.Run(ctx)
	for {
		select {
		case <-ctx.Done():
			t.Fatalf("jobs did not finish: %s", e.pendingSummary())
		default:
		}
		var cnt int64
		e.db.Model(&models.ReassignJob{}).Where("status IN ?", []string{"pending", "running"}).Count(&cnt)
		var failed int64
		e.db.Model(&models.ReassignJob{}).Where("status = 'failed'").Count(&failed)
		if failed > 0 {
			t.Fatalf("a job failed: %s", e.pendingSummary())
		}
		if cnt == 0 {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func (e *env) pendingSummary() string {
	var jobs []models.ReassignJob
	e.db.Where("status IN ?", []string{"pending", "running", "failed"}).Find(&jobs)
	s := ""
	for _, j := range jobs {
		s += fmt.Sprintf("[job %d org %d seq %d status %s err %s] ", j.ID, j.OrgID, j.TargetSeq, j.Status, j.Error)
	}
	return s
}

func (e *env) activeSeq(orgID int64) int64 {
	var o models.Organization
	e.db.First(&o, orgID)
	return o.ActiveSetSeq
}

func (e *env) point(orgID int64, ext string) models.Point {
	var p models.Point
	if err := e.db.Where("org_id = ? AND external_id = ?", orgID, ext).First(&p).Error; err != nil {
		return models.Point{}
	}
	return p
}

func (e *env) job(id int64) models.ReassignJob {
	var j models.ReassignJob
	e.db.First(&j, id)
	return j
}

// resetOrg wipes one organization's data for test isolation.
func (e *env) resetOrg(t *testing.T, label string) {
	id := e.orgs[label]
	e.db.Exec(`DELETE st FROM assignment_staging st JOIN points p ON p.id = st.point_id WHERE p.org_id = ?`, id)
	e.db.Exec(`DELETE FROM assignment_staging WHERE org_id = ?`, id)
	e.db.Exec(`DELETE FROM reassign_jobs WHERE org_id = ?`, id)
	e.db.Exec(`DELETE FROM points WHERE org_id = ?`, id)
	e.db.Exec(`DELETE rv FROM region_versions rv JOIN regions r ON r.id = rv.region_id WHERE r.org_id = ?`, id)
	e.db.Exec(`DELETE FROM regions WHERE org_id = ?`, id)
	e.db.Exec(`UPDATE organizations SET active_set_seq = 0 WHERE id = ?`, id)
}

// squarePolygon returns an axis-aligned square ring centred on (cx,cy).
func squarePolygon(cx, cy, h float64) []geometry.Vertex {
	return []geometry.Vertex{
		{Lng: cx - h, Lat: cy - h},
		{Lng: cx + h, Lat: cy - h},
		{Lng: cx + h, Lat: cy + h},
		{Lng: cx - h, Lat: cy + h},
	}
}

// Package tzdb 提供与宿主机无关的、固定版本的 IANA 时区数据库。
//
// 实现方式：把 IANA tz 数据库编译产物 zoneinfo.zip vendor 进仓库
// （文件：internal/tzdb/zoneinfo.zip），通过 go:embed 嵌入二进制，
// 运行时用 time.LoadLocationFromTZData 逐个解析，绝不读取宿主机的
// /usr/share/zoneinfo，也不依赖 time/tzdata 的隐式回退逻辑。
// 因此无论部署到哪台机器，时区规则（含夏令时切换点）都完全一致。
package tzdb

import (
	"archive/zip"
	"bytes"
	_ "embed"
	"fmt"
	"io"
	"sync"
	"time"
)

// Version 是内嵌 zoneinfo.zip 对应的 IANA tz 数据库发布版本。
// 该 zip 取自 Go 1.23.4 发行版（$GOROOT/lib/time/zoneinfo.zip）。
// 版本通过两条规则探针实测锁定（见 tzdb_test.go）：
//   - Asia/Almaty 2026 年为 UTC+5（哈萨克斯坦 2024-03 统一时区）→ ≥2024a
//   - America/Asuncion 2026 年 7 月仍为 UTC-4（巴拉圭夏令时仍存在）→ <2024b
const Version = "2024a"

// ZipSHA256 是内嵌 zoneinfo.zip 的 SHA-256，用于核验“固定版本”未被替换。
const ZipSHA256 = "2359012e2cf90a23bf4e9eeaeecc06b7936e9f37b76af97e9dc904b95d6a944a"

//go:embed zoneinfo.zip
var zipData []byte

var (
	once    sync.Once
	zones   map[string]*time.Location
	loadErr error
)

// LoadLocation 从内嵌的固定版本 tz 数据库加载时区。
// 未知名称返回错误；进程内只解析一次，后续查表返回。
func LoadLocation(name string) (*time.Location, error) {
	once.Do(func() {
		zones, loadErr = loadAll(zipData)
	})
	if loadErr != nil {
		return nil, loadErr
	}
	loc, ok := zones[name]
	if !ok {
		return nil, fmt.Errorf("未知时区 %q（内嵌 tz 数据库版本 %s）", name, Version)
	}
	return loc, nil
}

// loadAll 解析 zoneinfo.zip 中的全部 tzfile。
func loadAll(data []byte) (map[string]*time.Location, error) {
	zr, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return nil, fmt.Errorf("解析内嵌 zoneinfo.zip 失败: %w", err)
	}
	zones := make(map[string]*time.Location, len(zr.File))
	for _, f := range zr.File {
		if f.FileInfo().IsDir() {
			continue
		}
		rc, err := f.Open()
		if err != nil {
			return nil, fmt.Errorf("读取 %s 失败: %w", f.Name, err)
		}
		b, err := io.ReadAll(rc)
		rc.Close()
		if err != nil {
			return nil, fmt.Errorf("读取 %s 失败: %w", f.Name, err)
		}
		loc, err := time.LoadLocationFromTZData(f.Name, b)
		if err != nil {
			return nil, fmt.Errorf("解析时区 %s 失败: %w", f.Name, err)
		}
		zones[f.Name] = loc
	}
	return zones, nil
}

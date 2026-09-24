package tzdb

import (
	"testing"
	"time"
)

// 内嵌数据库必须独立于宿主机：直接验证若干已知规则。
func TestLoadKnownZones(t *testing.T) {
	for _, name := range []string{"America/New_York", "Asia/Shanghai", "UTC", "Australia/Lord_Howe", "Europe/Berlin"} {
		if _, err := LoadLocation(name); err != nil {
			t.Errorf("LoadLocation(%q) 失败: %v", name, err)
		}
	}
}

func TestUnknownZone(t *testing.T) {
	if _, err := LoadLocation("Mars/Olympus"); err == nil {
		t.Error("未知时区应报错")
	}
}

// 版本探针：用两条规则变化把内嵌数据库锁定为 2024a。
// 若 zip 被替换成其他版本，此测试必然失败。
func TestEmbeddedVersionProbe(t *testing.T) {
	// 探针一（≥2024a）：哈萨克斯坦 2024-03-01 起统一为 UTC+5，
	// 2023c 及更早版本中 Asia/Almaty 为 UTC+6。
	almaty, err := LoadLocation("Asia/Almaty")
	if err != nil {
		t.Fatal(err)
	}
	_, off := time.Date(2026, 1, 15, 12, 0, 0, 0, almaty).Zone()
	if off != 5*3600 {
		t.Errorf("2026-01 Almaty 偏移 = %d 秒，期望 +18000（UTC+5，tzdata ≥2024a）", off)
	}

	// 探针二（<2024b）：巴拉圭在 2024b 中永久取消夏令时；
	// 2024a 中 America/Asuncion 2026 年 7 月（南半球冬季）仍为 UTC-4。
	asu, err := LoadLocation("America/Asuncion")
	if err != nil {
		t.Fatal(err)
	}
	_, offJan := time.Date(2026, 1, 15, 12, 0, 0, 0, asu).Zone()
	_, offJul := time.Date(2026, 7, 15, 12, 0, 0, 0, asu).Zone()
	if offJan != -3*3600 || offJul != -4*3600 {
		t.Errorf("2026 Asuncion 偏移 1月=%d 7月=%d，期望 -10800/-14400（tzdata 2024a）", offJan, offJul)
	}
}

// 2026 年美国东部 DST 切换点（tzdata 2024b 规则）：
// 春季 2026-03-08 02:00 EST → 03:00 EDT；秋季 2026-11-01 02:00 EDT → 01:00 EST。
func TestNewYorkDST2026(t *testing.T) {
	loc, err := LoadLocation("America/New_York")
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		at       string // UTC
		wantOff  int    // 秒
		wantAbbr string
	}{
		{"2026-03-08T06:59:59Z", -5 * 3600, "EST"},
		{"2026-03-08T07:00:00Z", -4 * 3600, "EDT"},
		{"2026-11-01T05:59:59Z", -4 * 3600, "EDT"},
		{"2026-11-01T06:00:00Z", -5 * 3600, "EST"},
	}
	for _, c := range cases {
		ts, _ := time.Parse(time.RFC3339, c.at)
		abbr, off := ts.In(loc).Zone()
		if off != c.wantOff || abbr != c.wantAbbr {
			t.Errorf("%s → %s %d，期望 %s %d", c.at, abbr, off, c.wantAbbr, c.wantOff)
		}
	}
}

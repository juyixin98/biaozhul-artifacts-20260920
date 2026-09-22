package detection

import (
	"math"
	"testing"
)

func TestDecideZScore(t *testing.T) {
	p := ZScoreParams{LookbackDays: 30, ZThreshold: 2.5, MinSampleDays: 7, MinPositiveDays: 5}

	t.Run("样本不足-总天数不够", func(t *testing.T) {
		d := decideZScore([]float64{1, 1, 1}, 50, p)
		if d.Triggered {
			t.Fatal("should not trigger with insufficient sample days")
		}
		if d.Reason != "insufficient_sample" {
			t.Fatalf("reason = %q", d.Reason)
		}
	})

	t.Run("样本不足-正样本天数不够", func(t *testing.T) {
		hist := make([]float64, 30)
		hist[0], hist[1] = 4, 4 // 仅 2 个有事件日
		d := decideZScore(hist, 50, p)
		if d.Triggered || d.Reason != "insufficient_sample" {
			t.Fatalf("triggered=%v reason=%q", d.Triggered, d.Reason)
		}
	})

	t.Run("超过2.5个标准差触发", func(t *testing.T) {
		// 历史恒定每天 3 个，再加少量波动使 std>0；当日 20 个。
		hist := make([]float64, 29)
		for i := range hist {
			hist[i] = 3
		}
		hist = append(hist, 4)
		d := decideZScore(hist, 20, p)
		if !d.Triggered {
			t.Fatalf("should trigger: mean=%.2f std=%.3f z=%.2f", d.Mean, d.Std, d.Z)
		}
		if d.Z <= p.ZThreshold {
			t.Fatalf("z=%.3f > %.1f", d.Z, p.ZThreshold)
		}
	})

	t.Run("正常波动不触发", func(t *testing.T) {
		hist := []float64{10, 12, 9, 11, 10, 13, 9}
		d := decideZScore(hist, 12, p)
		if d.Triggered {
			t.Fatalf("normal value should not trigger, z=%.2f", d.Z)
		}
	})

	t.Run("零方差且高于恒定历史也触发", func(t *testing.T) {
		hist := make([]float64, 30)
		for i := range hist {
			hist[i] = 5
		}
		d := decideZScore(hist, 6, p)
		if !d.Triggered || !math.IsInf(d.Z, 1) {
			t.Fatalf("zero-variance increase should trigger, z=%v", d.Z)
		}
	})
}

func TestMeanStd(t *testing.T) {
	// 样本 [2,4,4,4,5,5,7,9]：均值 5，离差平方和 32，总体标准差为 2。
	mean, std := meanStd([]float64{2, 4, 4, 4, 5, 5, 7, 9})
	if math.Abs(mean-5) > 1e-9 {
		t.Fatalf("mean = %v, want 5", mean)
	}
	if math.Abs(std-math.Sqrt(32.0/7.0)) > 1e-9 {
		t.Fatalf("std = %v, want sqrt(32/7) (n-1)", std)
	}
}

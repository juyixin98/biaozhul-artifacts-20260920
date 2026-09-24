package scaler

import "fmt"

// 本文件集中管理决策理由（中文）的生成，保证理由文案一致、便于测试与审计。

func formatReady(n int, readyAt int64, totalReady int) string {
	return fmt.Sprintf("%d 个新副本在 t=%ds 冷启动完成并就绪，当前就绪副本=%d",
		n, readyAt, totalReady)
}

func formatPending(n int, readyAt int64) string {
	return fmt.Sprintf("有 %d 个新副本仍在冷启动中（预计 t=%ds 就绪），本拍不做扩缩容决策",
		n, readyAt)
}

func formatColdGate(now, cold int64) string {
	return fmt.Sprintf("处于初始冷启动窗口（t=%ds < %ds），不做扩缩容决策", now, cold)
}

func formatStale(latestAt, now, freshness int64) string {
	return fmt.Sprintf("最近样本为 t=%ds，距今 %ds 超过存活上限 %ds，按信号失效处理，保持副本数（不当零负载）",
		latestAt, now-latestAt, freshness)
}

func formatWindowBasis(direction, raw, target int, clamped bool, upN, downN int,
	upMax, downMax float64) string {
	switch direction {
	case +1:
		s := fmt.Sprintf("扩容窗口(%d 个样本)峰值负载 %.0f，对应期望 %d 副本，高于当前就绪数",
			upN, upMax, raw)
		if clamped {
			s += fmt.Sprintf("；期望值被上限钳制为 %d", target)
		}
		return s
	default:
		s := fmt.Sprintf("缩容窗口(%d 个样本)峰值负载 %.0f，对应期望 %d 副本，低于当前就绪数，且窗口完整",
			downN, downMax, raw)
		if clamped {
			s += fmt.Sprintf("；期望值被下限钳制为 %d", target)
		}
		return s
	}
}

// formatNoop 说明“两个窗口按各自语义都不触发动作”的原因。
func formatNoop(rawUp, rawDown, ready int) string {
	return fmt.Sprintf("扩容看短窗口峰值（期望 %d，未超过当前 %d）；缩容看长窗口峰值（期望 %d，未低于当前 %d），均不触发动作",
		rawUp, ready, rawDown, ready)
}

// formatDownBlocked 说明“长窗口期望更低，但因窗口不完整而拒绝缩容”的数值依据。
func formatDownBlocked(rawUp, rawDown, ready int) string {
	return fmt.Sprintf("扩容窗口期望 %d（未超过当前 %d）；缩容窗口期望 %d 虽低于当前 %d，但窗口不完整，本拍保持（绝不按不完整信号缩容）",
		rawUp, ready, rawDown, ready)
}

func formatHysteresis(up bool, ready, target int, threshold float64) string {
	if up {
		return fmt.Sprintf("期望 %d 相对当前 %d 的增幅未越过扩容迟滞阈值 %.0f%%，保持（迟滞带内）",
			target, ready, threshold)
	}
	return fmt.Sprintf("期望 %d 相对当前 %d 的降幅未越过缩容迟滞阈值 %.0f%%，保持（迟滞带内）",
		target, ready, threshold)
}

func formatCooldown(up bool, wait, cooldown int64) string {
	if up {
		return fmt.Sprintf("扩容冷却中，还需等待 %ds（冷却 %ds），本拍不扩容", wait, cooldown)
	}
	return fmt.Sprintf("缩容冷却中，还需等待 %ds（冷却 %ds），本拍不缩容", wait, cooldown)
}

func formatStabilization(wait, stab int64) string {
	return fmt.Sprintf("扩容后稳定窗口内，还需等待 %ds（稳定期 %ds），禁止缩容", wait, stab)
}

func formatUp(add, readyAfter, step int, cold, readyAt int64) string {
	return fmt.Sprintf("执行扩容 +%d（单步限速 %d），新副本进入 %ds 冷启动（t=%ds 就绪），就绪副本暂为 %d",
		add, step, cold, readyAt, readyAfter)
}

func formatDown(remove, readyAfter, step int, cooldown int64) string {
	return fmt.Sprintf("执行缩容 -%d（单步限速 %d），当前就绪副本=%d，缩容冷却 %ds",
		remove, step, readyAfter, cooldown)
}

"""端到端演示：不中断训练 vs 中断后从检查点恢复，对比最终参数与损失。

用法: python3 scripts/demo_resume.py [中断步数] [总步数]
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from checkpoint_resume import TrainConfig, Trainer


def main() -> int:
    interrupt_at = int(sys.argv[1]) if len(sys.argv) > 1 else 17
    total_steps = int(sys.argv[2]) if len(sys.argv) > 2 else 40

    base = Path(tempfile.mkdtemp(prefix="ckpt_demo_"))
    try:
        # 1) 不中断基准
        cfg_ref = TrainConfig(checkpoint_dir=str(base / "ref"), checkpoint_every=10**9)
        ref = Trainer(cfg_ref, resume=False)
        ref.train(total_steps)
        ref_loss = ref.full_loss()

        # 2) 中断 + 恢复（两个独立目录互不影响，使用各自的 checkpoint_dir）
        cfg_a = TrainConfig(checkpoint_dir=str(base / "run"), checkpoint_every=10**9)
        first = Trainer(cfg_a, resume=False)
        first.train(interrupt_at)
        saved = first.save()
        print(f"[中断] 训练 {interrupt_at} 步后保存检查点: {saved.name}")
        del first  # 模拟进程退出

        cfg_b = TrainConfig(checkpoint_dir=str(base / "run"), checkpoint_every=10**9)
        second = Trainer(cfg_b, resume=True)
        print(f"[恢复] 从 {second.resumed_from} 恢复，继续训练 {total_steps - interrupt_at} 步")
        second.train(total_steps - interrupt_at)
        resumed_loss = second.full_loss()

        # 3) 对比
        dw = np.max(np.abs(ref.model.w - second.model.w))
        db = np.max(np.abs(ref.model.b - second.model.b))
        dl = abs(ref_loss - resumed_loss)
        print(f"[对比] max|Δw|={dw:.3e}  max|Δb|={db:.3e}  |Δloss|={dl:.3e}")
        print(f"[损失] 不中断={ref_loss:.10f}  恢复={resumed_loss:.10f}")
        ok = dw == 0.0 and db == 0.0 and dl == 0.0
        print("[结论]", "逐位一致 OK" if ok else "存在差异 FAIL")
        return 0 if ok else 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

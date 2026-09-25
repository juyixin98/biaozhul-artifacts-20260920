"""断点恢复一致性：在不同批次中断，恢复后与不中断训练逐位一致。

约定容差：本实现中恢复路径与不中断路径执行完全相同的浮点运算序列，
因此容差取 0（逐位相等），比「明确容差内一致」更强。
"""

import numpy as np
import pytest

from checkpoint_resume import TrainConfig, Trainer

TOTAL_STEPS = 40  # 每 epoch 16 步（512/32），覆盖 2.5 个 epoch


def _cfg(tmp_path, name):
    return TrainConfig(
        checkpoint_dir=str(tmp_path / name),
        checkpoint_every=10**9,  # 关闭自动保存，手动控制保存时机
    )


def _run_uninterrupted(tmp_path):
    trainer = Trainer(_cfg(tmp_path, "ref"), resume=False)
    trainer.train(TOTAL_STEPS)
    return trainer


@pytest.fixture(scope="module")
def reference(tmp_path_factory):
    return _run_uninterrupted(tmp_path_factory.mktemp("ref"))


# 中断点覆盖：epoch 中途、epoch 边界（16/32 恰好是批次末尾）、跨 epoch
@pytest.mark.parametrize("interrupt_at", [1, 5, 15, 16, 17, 23, 32, 33, 39])
def test_resume_matches_uninterrupted(reference, tmp_path, interrupt_at):
    first = Trainer(_cfg(tmp_path, "run"), resume=False)
    first.train(interrupt_at)
    first.save()
    del first  # 模拟进程被杀

    second = Trainer(_cfg(tmp_path, "run"), resume=True)
    assert second.global_step == interrupt_at
    second.train(TOTAL_STEPS - interrupt_at)

    np.testing.assert_array_equal(second.model.w, reference.model.w)
    np.testing.assert_array_equal(second.model.b, reference.model.b)
    assert second.full_loss() == reference.full_loss()


@pytest.mark.parametrize("interrupt_at", [7, 16, 32])
def test_batch_sequence_identical(reference, tmp_path, interrupt_at):
    """恢复后消费的批次序列（按样本索引哈希）必须与不中断完全一致：
    不重复、不遗漏、不乱序。"""
    first = Trainer(_cfg(tmp_path, "run"), resume=False)
    first.train(interrupt_at)
    first.save()
    del first

    second = Trainer(_cfg(tmp_path, "run"), resume=True)
    second.train(TOTAL_STEPS - interrupt_at)

    resumed_hashes = [r["batch_hash"] for r in second.history]
    reference_hashes = [r["batch_hash"] for r in reference.history[interrupt_at:]]
    assert resumed_hashes == reference_hashes


def test_optimizer_state_restored(reference, tmp_path):
    """恢复点的优化器动量缓冲必须与不中断运行在同一时刻一致。"""
    interrupt_at = 20
    first = Trainer(_cfg(tmp_path, "run"), resume=False)
    first.train(interrupt_at)
    first.save()
    del first

    second = Trainer(_cfg(tmp_path, "run"), resume=True)
    np.testing.assert_array_equal(second.optimizer.v_w, _v_w_at(reference, interrupt_at))


def _v_w_at(trainer, step):
    # reference 的 history 不含中间优化器快照，这里重新跑到指定步取动量
    # （仅测试辅助：从同一检查点目录重放，保证确定性）
    cfg = TrainConfig(
        checkpoint_dir=trainer.config.checkpoint_dir,
        checkpoint_every=10**9,
    )
    replay = Trainer(cfg, resume=False)
    replay.train(step)
    return replay.optimizer.v_w

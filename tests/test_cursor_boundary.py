"""数据游标边界：检查点恰好落在 epoch 末尾（next_batch == n_batches）时，
恢复后必须进入下一 epoch，而不是重复旧 epoch 的批次或跳过新 epoch 的批次。
"""

import numpy as np

from checkpoint_resume import TrainConfig, Trainer

STEPS_PER_EPOCH = 512 // 32  # 16


def _cfg(tmp_path, name):
    return TrainConfig(checkpoint_dir=str(tmp_path / name), checkpoint_every=10**9)


def test_cursor_at_epoch_boundary(tmp_path):
    trainer = Trainer(_cfg(tmp_path, "run"), resume=False)
    trainer.train(STEPS_PER_EPOCH)  # 恰好跑完 epoch 1
    assert trainer.loader.epoch == 1
    assert trainer.loader.next_batch == STEPS_PER_EPOCH  # 游标停在批次末尾
    trainer.save()


def test_resume_at_epoch_boundary_continues_into_next_epoch(tmp_path):
    # 基准：不中断跑 2 个 epoch
    ref = Trainer(_cfg(tmp_path, "ref"), resume=False)
    ref.train(2 * STEPS_PER_EPOCH)

    # 中断点恰好是 epoch 末尾
    first = Trainer(_cfg(tmp_path, "run"), resume=False)
    first.train(STEPS_PER_EPOCH)
    first.save()
    del first

    second = Trainer(_cfg(tmp_path, "run"), resume=True)
    assert second.loader.epoch == 1
    assert second.loader.next_batch == STEPS_PER_EPOCH

    record = second.step()  # 恢复后的第一个批次
    assert record["epoch"] == 2
    assert record["batch_idx"] == 0
    # 该批次必须与不中断运行 epoch 2 的第一个批次完全相同
    assert record["batch_hash"] == ref.history[STEPS_PER_EPOCH]["batch_hash"]

    second.train(STEPS_PER_EPOCH - 1)
    np.testing.assert_array_equal(second.model.w, ref.model.w)
    assert second.full_loss() == ref.full_loss()


def test_no_batch_repeated_or_skipped_across_epoch_boundary(tmp_path):
    total = 2 * STEPS_PER_EPOCH + 3
    ref = Trainer(_cfg(tmp_path, "ref"), resume=False)
    ref.train(total)

    first = Trainer(_cfg(tmp_path, "run"), resume=False)
    first.train(STEPS_PER_EPOCH)
    first.save()
    del first

    second = Trainer(_cfg(tmp_path, "run"), resume=True)
    second.train(total - STEPS_PER_EPOCH)

    resumed_hashes = [r["batch_hash"] for r in second.history]
    ref_hashes = [r["batch_hash"] for r in ref.history[STEPS_PER_EPOCH:]]
    assert resumed_hashes == ref_hashes
    assert len(set(resumed_hashes[:STEPS_PER_EPOCH])) == STEPS_PER_EPOCH  # 无重复


def test_cursor_saved_mid_epoch(tmp_path):
    trainer = Trainer(_cfg(tmp_path, "run"), resume=False)
    trainer.train(7)
    state = trainer.state_dict()
    assert int(state["epoch"]) == 1
    assert int(state["next_batch"]) == 7
    assert state["perm"].shape == (512,)

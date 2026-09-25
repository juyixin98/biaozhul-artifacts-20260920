"""检查点完整性：损坏检测、回退、以及「禁止只存权重」的内容约束。"""

import numpy as np
import pytest

from checkpoint_resume import CorruptCheckpointError, TrainConfig, Trainer
from checkpoint_resume.checkpoint import (
    find_latest_valid,
    list_checkpoints,
    load_checkpoint,
    sha_path_for,
    verify_checkpoint,
)


def _cfg(tmp_path, name="run", keep_last=3):
    return TrainConfig(
        checkpoint_dir=str(tmp_path / name),
        checkpoint_every=10**9,
        keep_last=keep_last,
    )


def _train_and_save(tmp_path, steps, name="run", keep_last=3):
    trainer = Trainer(_cfg(tmp_path, name, keep_last), resume=False)
    trainer.train(steps)
    path = trainer.save()
    return trainer, path


def test_save_load_roundtrip(tmp_path):
    trainer, path = _train_and_save(tmp_path, 5)
    state, meta = load_checkpoint(path)
    np.testing.assert_array_equal(state["model_w"], trainer.model.w)
    np.testing.assert_array_equal(state["opt_vw"], trainer.optimizer.v_w)
    assert int(state["global_step"]) == 5
    assert meta["config"]["seed"] == trainer.config.seed


def test_checkpoint_contains_full_state_not_weights_only(tmp_path):
    """检查点必须包含：模型参数、优化器状态、RNG 状态、数据游标。"""
    _, path = _train_and_save(tmp_path, 5)
    state, _ = load_checkpoint(path)
    required = {
        "model_w", "model_b",          # 模型参数
        "opt_vw", "opt_vb",            # 优化器动量状态
        "rng_keys", "rng_pos",         # 随机数状态
        "epoch", "next_batch", "perm",  # 数据游标 + 当前 epoch 排列
        "global_step",
    }
    missing = required - set(state)
    assert not missing, f"检查点缺少字段（不允许只存权重）: {missing}"


def test_dropping_optimizer_state_changes_trajectory(tmp_path):
    """反向验证：只恢复权重、丢掉动量，轨迹必然偏离 ——
    证明优化器状态是检查点的必要组成部分。"""
    interrupt_at, total = 10, 25
    ref = Trainer(_cfg(tmp_path, "ref"), resume=False)
    ref.train(total)

    first = Trainer(_cfg(tmp_path, "run"), resume=False)
    first.train(interrupt_at)
    saved_model = first.model.params()
    del first

    # 只带权重「恢复」：新 Trainer 的动量从零开始
    weights_only = Trainer(_cfg(tmp_path, "other"), resume=False)
    weights_only.model.load_state_dict(
        {"model_w": saved_model["w"], "model_b": saved_model["b"]}
    )
    weights_only.global_step = interrupt_at
    weights_only.train(total - interrupt_at)

    assert not np.allclose(weights_only.model.w, ref.model.w, rtol=0, atol=1e-12)


def test_corrupted_checkpoint_detected(tmp_path):
    _, path = _train_and_save(tmp_path, 5)
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF  # 翻转中间一个字节
    path.write_bytes(bytes(data))
    with pytest.raises(CorruptCheckpointError, match="SHA-256"):
        verify_checkpoint(path)
    with pytest.raises(CorruptCheckpointError):
        load_checkpoint(path)


def test_missing_sha_file_detected(tmp_path):
    _, path = _train_and_save(tmp_path, 5)
    sha_path_for(path).unlink()
    with pytest.raises(CorruptCheckpointError, match="校验文件"):
        verify_checkpoint(path)


def test_garbage_checkpoint_detected(tmp_path):
    _, path = _train_and_save(tmp_path, 5)
    path.write_bytes(b"this is not an npz file")
    # 校验和先拦下；即使伪造校验和，解析也必须失败
    with pytest.raises(CorruptCheckpointError):
        load_checkpoint(path)


def test_trainer_falls_back_to_previous_valid_checkpoint(tmp_path):
    cfg = _cfg(tmp_path, keep_last=10)
    trainer = Trainer(cfg, resume=False)
    trainer.train(5)
    trainer.save()
    trainer.train(5)  # global_step = 10
    latest = trainer.save()
    assert len(list_checkpoints(cfg.checkpoint_dir)) == 2

    # 损坏最新检查点 → 应回退到 step=5 的检查点
    data = bytearray(latest.read_bytes())
    data[100] ^= 0xFF
    latest.write_bytes(bytes(data))

    resumed = Trainer(cfg, resume=True)
    assert resumed.global_step == 5
    assert resumed.resumed_from is not None
    assert "step_00000005" in resumed.resumed_from


def test_all_checkpoints_corrupt_starts_fresh(tmp_path):
    cfg = _cfg(tmp_path)
    trainer = Trainer(cfg, resume=False)
    trainer.train(5)
    path = trainer.save()
    path.write_bytes(b"corrupted")
    assert find_latest_valid(cfg.checkpoint_dir) is None
    fresh = Trainer(cfg, resume=True)
    assert fresh.global_step == 0
    assert fresh.resumed_from is None


def test_config_mismatch_rejected(tmp_path):
    _train_and_save(tmp_path, 5)
    bad_cfg = TrainConfig(
        checkpoint_dir=str(tmp_path / "run"),
        checkpoint_every=10**9,
        batch_size=64,  # 与检查点不一致
    )
    with pytest.raises(ValueError, match="不匹配"):
        Trainer(bad_cfg, resume=True)


def test_prune_keeps_only_last_n(tmp_path):
    cfg = _cfg(tmp_path, keep_last=2)
    trainer = Trainer(cfg, resume=False)
    for _ in range(4):
        trainer.train(3)
        trainer.save()
    remaining = list_checkpoints(cfg.checkpoint_dir)
    assert len(remaining) == 2
    assert remaining[-1].name == "step_00000012.ckpt.npz"
    for p in remaining:
        verify_checkpoint(p)  # 不抛异常即通过

"""计算图测试：解析梯度对照独立有限差分；空分片边界；分片性质。"""

from __future__ import annotations

import numpy as np
import pytest

from gradbucket.graph import (
    LinearModel,
    ModelConfig,
    make_dataset,
    shard_indices,
)

from conftest import finite_difference_grad


def test_dataset_is_deterministic_and_partitionable():
    x1, y1 = make_dataset(12, seed=445)
    x2, y2 = make_dataset(12, seed=445)
    np.testing.assert_array_equal(x1, x2)
    np.testing.assert_array_equal(y1, y2)
    x3, _ = make_dataset(12, seed=446)
    assert not np.array_equal(x1, x3)


def test_uniform_shards_partition_all_samples_without_overlap():
    parts = [shard_indices(12, r, 3, seed=445) for r in range(3)]
    assert sorted(np.concatenate(parts).tolist()) == list(range(12))
    assert [len(p) for p in parts] == [4, 4, 4]


def test_explicit_shard_sizes_are_honored_exactly():
    sizes = [2, 3, 7]
    parts = [shard_indices(12, r, 3, shard_sizes=sizes, seed=445) for r in range(3)]
    assert [len(p) for p in parts] == sizes
    assert sorted(np.concatenate(parts).tolist()) == list(range(12))


def test_bad_shard_sizes_rejected():
    with pytest.raises(ValueError, match="shard_sizes"):
        shard_indices(12, 0, 3, shard_sizes=[1, 1, 9], seed=445)  # 和=11
    with pytest.raises(ValueError, match="越界"):
        shard_indices(12, 5, 3)


def test_analytic_gradients_match_finite_differences(cfg, data):
    x, y = data
    model = LinearModel(cfg)
    # 用非零权重检验，避免在零点的巧合一致。
    rng = np.random.default_rng(7)
    model.set_weights(rng.normal(size=(2, 3)), rng.normal(size=2))
    grads, n = model.gradients(x, y)
    assert n == 12
    for name in ["w", "b"]:
        fd = finite_difference_grad(model, x, y, name, eps=1e-6)
        np.testing.assert_allclose(grads[name], fd, rtol=1e-6, atol=1e-8,
                                   err_msg=f"{name} 解析梯度与有限差分不符")


def test_spare_gradient_is_structurally_zero(model, data):
    x, y = data
    grads, _ = model.gradients(x, y)
    np.testing.assert_array_equal(grads["spare"], np.zeros(2))


def test_gradient_is_average_not_sum_specific_values(model, data):
    # 单样本与双样本分片上，平均梯度定义不同：用具体数值固定该性质。
    x, y = data
    g1, n1 = model.gradients(x[:1], y[:1])
    g2, n2 = model.gradients(x[:2], y[:2])
    assert n1 == 1 and n2 == 2
    delta0 = -(y[0])  # W=b=0
    np.testing.assert_allclose(g1["b"], delta0, atol=1e-12)
    np.testing.assert_allclose(g2["b"], (delta0 + (-y[1])) / 2, atol=1e-12)


def test_empty_shard_is_rejected_not_treated_as_zero_gradient(model):
    with pytest.raises(ValueError, match="空分片"):
        model.gradients(np.zeros((0, 3)), np.zeros((0, 2)))


def test_shape_validation(model):
    with pytest.raises(ValueError, match="形状"):
        model.gradients(np.zeros((2, 5)), np.zeros((2, 2)))
    with pytest.raises(ValueError, match="形状"):
        model.gradients(np.zeros((2, 3)), np.zeros((2, 3)))


def test_loss_decreases_after_one_agreement_step(model, data):
    x, y = data
    before = model.loss(x, y)
    grads, _ = model.gradients(x, y)
    model.w -= 0.1 * grads["w"]
    model.b -= 0.1 * grads["b"]
    after = model.loss(x, y)
    assert after < before - 1e-9

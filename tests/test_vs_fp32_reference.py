"""fp16 training vs an independent fp32 reference implemented in the test.

The reference below is a self-contained fp64 training loop written in this
test file (not imported from the package), so the comparison is a genuine
cross-check of the mixed-precision core rather than self-agreement.
"""

import numpy as np

from mptrainer import graph
from mptrainer.config import TrainerConfig
from mptrainer.trainer import MixedPrecisionTrainer


def reference_fp32_run(w0, b0, batches, lr, momentum):
    """Plain fp64 SGD+momentum on the linear model; independent reference."""
    w, b = w0.astype(np.float64), b0.astype(np.float64)
    v_w, v_b = np.zeros_like(w), np.zeros_like(b)
    losses = []
    for x, y in batches:
        pred = x @ w + b
        diff = pred - y
        losses.append(float(np.mean(diff * diff)))
        g_w = (2.0 / len(x)) * (x.T @ diff)
        g_b = (2.0 / len(x)) * diff.sum(axis=0)
        v_w = momentum * v_w + g_w
        v_b = momentum * v_b + g_b
        w = w - lr * v_w
        b = b - lr * v_b
    return w, b, losses


def make_trainer(low_dtype: str) -> MixedPrecisionTrainer:
    config = TrainerConfig(
        layer_sizes=[4, 1], seed=77, base_lr=0.05, lr_decay=0.0,
        momentum=0.9, accum_steps=1, low_dtype=low_dtype,
        init_scale=256.0, growth_interval=10**9,
    ).validate()
    return MixedPrecisionTrainer(config, run_id=f"test-vs-fp32-{low_dtype}")


def test_fp16_tracks_fp32_reference_on_clean_data():
    lr, momentum = 0.05, 0.9
    fp16 = make_trainer("float16")
    fp32 = make_trainer("float32")
    w0 = fp16.master["W0"].copy()
    b0 = fp16.master["b0"].copy()

    rng = np.random.default_rng(31)
    batches = [
        (rng.standard_normal((8, 4)).astype(np.float32),
         rng.standard_normal((8, 1)).astype(np.float32))
        for _ in range(20)
    ]
    fp16_losses, fp32_losses = [], []
    for x, y in batches:
        fp16_losses.append(fp16.train_step(x, y).loss)
        fp32_losses.append(fp32.train_step(x, y).loss)

    ref_w, ref_b, ref_losses = reference_fp32_run(w0, b0, batches, lr, momentum)

    # The trainer's own fp32 mode must match the independent reference tightly.
    np.testing.assert_allclose(fp32.master["W0"].astype(np.float64), ref_w, rtol=1e-4)
    # fp16 accumulates rounding error but must stay close to the reference.
    err_w = np.max(np.abs(fp16.master["W0"].astype(np.float64) - ref_w))
    err_b = np.max(np.abs(fp16.master["b0"].astype(np.float64) - ref_b))
    assert err_w < 2e-3, f"fp16 master drifted {err_w} from fp32 reference"
    assert err_b < 2e-3, f"fp16 bias drifted {err_b} from fp32 reference"
    # Both runs actually learned: final loss well below initial loss.
    assert fp16_losses[-1] < 0.5 * fp16_losses[0]
    assert ref_losses[-1] < 0.5 * ref_losses[0]


def test_loss_scale_cancels_numerically():
    """Same run at two loss scales: committed masters must agree closely."""
    def run(scale):
        config = TrainerConfig(
            layer_sizes=[4, 1], seed=77, base_lr=0.05, lr_decay=0.0,
            momentum=0.0, accum_steps=1, low_dtype="float32",
            init_scale=scale, growth_interval=10**9,
        ).validate()
        trainer = MixedPrecisionTrainer(config, run_id=f"scale-{scale}")
        rng = np.random.default_rng(41)
        for _ in range(5):
            x = rng.standard_normal((8, 4)).astype(np.float32)
            y = rng.standard_normal((8, 1)).astype(np.float32)
            trainer.train_step(x, y)
        return trainer.master

    a = run(1.0)
    b = run(2048.0)
    for k in a:
        np.testing.assert_allclose(a[k], b[k], rtol=1e-5, atol=1e-7)

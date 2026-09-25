"""Observable concurrency demonstration (no test framework involved).

Run alongside a registry produced by ``seed_artifacts.py``::

    python scripts/demo_concurrency.py --root ./artifacts

It proves, with printed evidence:
  1. an in-flight request that started on v1 keeps computing v1 results even
     after the active version becomes v2;
  2. the retired v1 buffers are NOT freed until that lease ends (refcount);
  3. a second switch attempted while one is loading fails fast with
     SwitchBusyError (or waits, depending on timeout) and never corrupts
     state;
  4. after the lease releases, v1's NumPy buffers are disposed.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_switch.errors import SwitchBusyError  # noqa: E402
from model_switch.loader import ArtifactLoader  # noqa: E402
from model_switch.manager import ModelManager  # noqa: E402


class SlowLoader(ArtifactLoader):
    """Adds a small artificial delay to load() so switches don't finish
    before the competing thread attempts one."""

    def __init__(self, root, delay=0.4):
        super().__init__(root)
        self.delay = delay

    def load(self, version):
        time.sleep(self.delay)
        return super().load(version)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="artifacts")
    args = parser.parse_args()

    mgr = ModelManager(SlowLoader(args.root, delay=0.4), switch_timeout_s=0.1)
    mgr.switch_to("v1")
    x = np.linspace(-1.0, 1.0, 4, dtype=np.float32)

    print(f"[start] active={mgr.active_version} gen={mgr.generation}")

    with mgr.lease() as lease:
        print(f"[in-flight] request started on {lease.version}, holding it open ...")

        # Trigger v1 -> v2 while the request is still in flight.
        switch_done = threading.Event()

        def do_switch():
            result = mgr.switch_to("v2")
            print(f"[switch] v1 -> {result.version} (gen {result.generation}), "
                  f"retired v1 refcount={result.retired_refcount}")
            switch_done.set()

        t = threading.Thread(target=do_switch)
        t.start()
        time.sleep(0.15)  # let the switch enter its slow load

        # While switch #1 is mid-load, a concurrent switch must fail fast.
        try:
            mgr.switch_to("v3")
            print("[concurrent] UNEXPECTED: second switch did not raise")
        except SwitchBusyError as exc:
            print(f"[concurrent] second switch rejected fast: SwitchBusyError ({exc})")

        switch_done.wait(timeout=5)
        t.join()

        status = mgr.status()
        print(f"[state] active={status['active_version']} gen={status['generation']} "
              f"retired={[(r['version'], r['refcount'], r['disposed']) for r in status['retired']]}")

        # The in-flight request STILL uses v1 weights.
        old_logits = lease.loaded.model.forward(x)
        new_pred = mgr.predict(x)
        print(f"[in-flight] held lease still computes as v1: {old_logits.tolist()}")
        print(f"[new req]  new request is served by    {new_pred.version}: "
              f"{new_pred.logits.tolist()}")
        assert lease.version == "v1"
        assert new_pred.version == "v2"
        assert not np.allclose(old_logits, new_pred.logits), "versions must differ"
        assert not lease.loaded.disposed, "in-flight buffers must still be alive"

    # Lease context exited -> refcount zero -> deferred release happens now.
    mgr.drain_retired(timeout_s=2.0)
    status = mgr.status()
    print(f"[release] after lease ended, retired={status['retired']} "
          f"(v1 disposed)")
    assert status["retired"] == []
    print("\nALL CONCURRENCY ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

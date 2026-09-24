"""End-to-end test on a local Anvil chain.

Deploy BoxV1 behind the proxy, write data, verify the layout checker
approves BoxV1 -> BoxV2, upgrade on-chain, and confirm the original data
survives while the new logic becomes active.
"""

from __future__ import annotations

from backend.artifacts import get_artifact, get_layout
from backend.chain import TEST_ADDRESS, connect, deploy, transact
from backend.layout_checker import check_layouts


def test_upgrade_preserves_storage(anvil, compiled_artifacts):
    w3 = connect(anvil)

    # 1. Deploy implementation V1 and the proxy pointing at it.
    box_v1 = deploy(w3, get_artifact("BoxV1"))
    proxy = deploy(w3, get_artifact("UpgradeableProxy"), box_v1.address, TEST_ADDRESS)
    assert proxy.functions.implementation().call() == box_v1.address

    # 2. Talk to the proxy through the V1 ABI and write state.
    box = w3.eth.contract(address=proxy.address, abi=get_artifact("BoxV1")["abi"])
    transact(w3, box.functions.initialize, 42, "hello-layout")
    transact(w3, box.functions.setValue, 1337)
    assert box.functions.value().call() == 1337
    assert box.functions.name().call() == "hello-layout"
    assert box.functions.owner().call() == TEST_ADDRESS
    assert box.functions.version().call() == "v1"

    # 3. Gate the upgrade on the layout checker (as an operator would).
    report = check_layouts(get_layout("BoxV1"), get_layout("BoxV2"), "BoxV1", "BoxV2")
    assert report.compatible, [i.message for i in report.errors]

    # 4. Deploy V2 and upgrade the proxy.
    box_v2 = deploy(w3, get_artifact("BoxV2"))
    transact(w3, proxy.functions.upgradeTo, box_v2.address)
    assert proxy.functions.implementation().call() == box_v2.address

    # 5. Original data must be intact; new logic must be live.
    box2 = w3.eth.contract(address=proxy.address, abi=get_artifact("BoxV2")["abi"])
    assert box2.functions.value().call() == 1337
    assert box2.functions.name().call() == "hello-layout"
    assert box2.functions.owner().call() == TEST_ADDRESS
    assert box2.functions.version().call() == "v2"

    # 6. New (appended) storage works through the upgraded proxy.
    transact(w3, box2.functions.setExtra, 7)
    assert box2.functions.extra().call() == 7


def test_checker_blocks_incompatible_upgrade(compiled_artifacts):
    """The reorder variant must be rejected before any upgrade is attempted."""
    report = check_layouts(
        get_layout("BoxV1"), get_layout("BoxBadReorder"), "BoxV1", "BoxBadReorder"
    )
    assert not report.compatible

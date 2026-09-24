"""Acceptance tests: confirmation depth, two-layer reorg rollback, catch-up,
and same-transaction-different-block disambiguation against a real Anvil.
"""
from app import onchain


def test_forward_sync_materializes_state(deployed):
    d = deployed
    d.deposit(d.user1_key, 100)
    onchain.mine_blocks(d.w3, 1)  # block 2
    d.withdraw(d.user1_key, 30)
    d.deposit(d.user2_key, 50)
    onchain.mine_blocks(d.w3, 1)  # block 3

    report = d.indexer.sync_to(3)
    assert report.rolled_back == []
    # blocks 0 (genesis anchor) and 1 (deploy) carry no Vault events; blocks
    # 2..3 do. report.applied lists every block caught up after the anchor.
    assert report.applied == [1, 2, 3]
    assert d.store.get_balance(d.user1) == 70
    assert d.store.get_balance(d.user2) == 50
    assert d.store.get_total("deposited") == 150
    assert d.store.get_total("withdrawn") == 30


def test_confirmation_depth_delays_indexing(deployed_confirmed):
    d = deployed_confirmed  # depth = 1
    d.deposit(d.user1_key, 999)
    onchain.mine_blocks(d.w3, 1)  # block 2 (head)
    target = d.indexer.target_head()
    assert target == 1  # block 2 not yet confirmed
    rep = d.indexer.poll_once()
    assert rep.target_number == 1
    assert d.store.event_count() == 0  # only genesis + deploy block indexed
    onchain.mine_blocks(d.w3, 1)  # block 3 -> block 2 confirmed
    rep = d.indexer.poll_once()
    assert rep.target_number == 2
    assert d.store.get_balance(d.user1) == 999


def test_two_layer_reorg_rollback_and_catchup(deployed):
    """Two-level reorg: blocks 5 and 6 are replaced wholesale."""
    d = deployed
    w3 = d.w3
    idx = d.indexer
    idx.sync_to(1)  # anchor through deploy

    # --- base chain blocks 2..4 ---
    d.deposit(d.user1_key, 100)
    onchain.mine_blocks(w3, 1)  # 2
    d.deposit(d.user1_key, 100)
    onchain.mine_blocks(w3, 1)  # 3
    idx.sync_to(3)
    assert d.store.get_balance(d.user1) == 200

    snap3 = onchain.snapshot(w3)

    # block 4u: user1's nonce-3 50-deposit on the (losing) uncle branch.
    t0 = w3.eth.get_block(3)["timestamp"]
    same_tx = d.deposit(d.user1_key, 50)
    onchain.mine_blocks(w3, 1, timestamps=[t0 + 100])  # block 4u
    idx.sync_to(4)
    assert d.store.get_balance(d.user1) == 250

    # --- uncle branch: blocks 5u,6u ---
    d.deposit(d.user1_key, 100)
    onchain.mine_blocks(w3, 1, timestamps=[t0 + 101])  # 5u
    d.deposit(d.user2_key, 70)
    onchain.mine_blocks(w3, 1, timestamps=[t0 + 102])  # 6u
    uncle = {
        4: onchain.block_info(w3, 4),
        5: onchain.block_info(w3, 5),
        6: onchain.block_info(w3, 6),
    }
    rep = idx.sync_to(6)
    assert rep.rolled_back == []
    assert d.store.get_balance(d.user1) == 350
    assert d.store.get_balance(d.user2) == 70

    # --- reorg back to 3, build canonical 4',5',6' ---
    assert onchain.revert(w3, snap3) is True
    assert w3.eth.block_number == 3
    # evm_revert rewinds account nonces; drop cached ones before re-sending.
    d.sender.reset_nonce_tracking()

    # canonical block 4': the identical nonce-3 transaction -> same tx hash,
    # but packaged at a different timestamp -> a different block hash.
    same_tx_again = d.deposit(d.user1_key, 50)
    assert same_tx_again == same_tx, "identical tx must hash identically across the fork"
    onchain.mine_blocks(w3, 1, timestamps=[t0 + 200])  # 4c
    d.deposit(d.user1_key, 200)
    onchain.mine_blocks(w3, 1, timestamps=[t0 + 201])  # 5c
    d.deposit(d.user2_key, 25)
    onchain.mine_blocks(w3, 1, timestamps=[t0 + 202])  # 6c

    canon = {
        4: onchain.block_info(w3, 4),
        5: onchain.block_info(w3, 5),
        6: onchain.block_info(w3, 6),
    }
    # block 4: identical tx, different block hash
    uncle4_hash = uncle[4]["hash"]
    assert uncle4_hash != canon[4]["hash"]
    assert uncle[5]["hash"] != canon[5]["hash"]
    assert uncle[6]["hash"] != canon[6]["hash"]
    assert canon[4]["parent_hash"] == onchain.block_info(w3, 3)["hash"]
    assert canon[5]["parent_hash"] == canon[4]["hash"]
    assert canon[6]["parent_hash"] == canon[5]["hash"]

    # --- the sync under test: roll back uncle blocks 4,5,6 and catch up -----
    rep = idx.sync_to(6)
    assert rep.reorged
    assert sorted(rep.rolled_back) == [4, 5, 6], rep
    assert rep.fork_point == 3
    assert rep.events_undone == 3          # shared-50, uncle-100, uncle-70
    assert rep.events_applied == 3         # block4'(50) + 5'(200) + 6'(25)
    assert sorted(rep.applied) == [4, 5, 6]

    # 1) business state equals the canonical chain, not the uncle
    assert d.store.get_balance(d.user1) == 200 + 50 + 200
    assert d.store.get_balance(d.user2) == 25
    assert d.store.get_total("deposited") == 200 + 50 + 200 + 25
    assert d.store.get_total("withdrawn") == 0

    # 2) uncle block rows and events are physically gone
    dbg = d.store.debug_rows()
    indexed_numbers = [b["number"] for b in dbg["blocks"]]
    indexed_hashes = {b["hash"] for b in dbg["blocks"]}
    assert indexed_numbers == [0, 1, 2, 3, 4, 5, 6]
    assert uncle4_hash not in indexed_hashes
    assert uncle[5]["hash"] not in indexed_hashes
    assert uncle[6]["hash"] not in indexed_hashes
    assert canon[4]["hash"] in indexed_hashes
    assert canon[5]["hash"] in indexed_hashes
    assert canon[6]["hash"] in indexed_hashes
    stored_events = d.store.list_events(limit=100)
    stored_pairs = {(e.block_hash, e.tx_hash, e.log_index) for e in stored_events}
    # uncle events removed
    assert all(e.block_hash != uncle4_hash for e in stored_events)
    assert all(e.block_hash != uncle[5]["hash"] for e in stored_events)
    assert all(e.block_hash != uncle[6]["hash"] for e in stored_events)
    # canonical events present, in correct block positions
    canon4_logs = onchain.vault_logs(w3, d.vault.address, 4, 4)
    assert len(canon4_logs) == 1
    # 3) same tx hash lives ONLY under the canonical block 4 now: on the uncle
    #    chain it was also block 4, but under a different block hash. The two
    #    must never be conflated -- the key includes the block hash.
    same_rows = [e for e in stored_events if e.tx_hash == same_tx]
    assert len(same_rows) == 1
    assert same_rows[0].block_hash == canon[4]["hash"]
    assert same_rows[0].block_number == 4
    # and the now-deleted uncle position is absent
    assert (uncle4_hash, same_tx, 0) not in stored_pairs

    # hash-linked parent chain is canonical end-to-end
    for n in (4, 5, 6):
        row = d.store.get_block(n)
        assert row.hash == canon[n]["hash"]
    assert d.store.get_block(6).parent_hash == canon[5]["hash"]


def test_restart_replays_canonical_chain(deployed):
    """After reorg, a fresh indexer over the same canonical chain must arrive
    at identical state/structure (canonical-chain replay equivalence)."""
    d = deployed
    w3 = d.w3
    idx = d.indexer
    idx.sync_to(1)

    d.deposit(d.user1_key, 100)
    onchain.mine_blocks(w3, 1)  # 2
    snap2 = onchain.snapshot(w3)
    d.deposit(d.user2_key, 777)  # uncle-only value
    onchain.mine_blocks(w3, 1)  # 3u
    idx.sync_to(3)
    assert d.store.get_balance(d.user2) == 777

    onchain.revert(w3, snap2)
    d.deposit(d.user1_key, 5)
    onchain.mine_blocks(w3, 1)  # 3c
    d.withdraw(d.user1_key, 20)
    onchain.mine_blocks(w3, 1)  # 4c
    idx.sync_to(4)

    # Snapshot of the reorged, converged indexer.
    before = d.store.debug_rows()
    before_events = sorted(
        (e.block_number, e.block_hash, e.tx_hash, e.log_index, e.event_name, e.who, e.amount)
        for e in d.store.list_events(limit=1000)
    )

    # "Restart": brand-new empty store, replay canonical chain from genesis.
    from app.storage import Storage as S2
    from app.indexer import Indexer as I2

    store2 = S2(":memory:")
    indexer2 = I2(w3, store2, vault_address=d.vault.address, start_block=0)
    rep = indexer2.sync_to(w3.eth.block_number)
    assert rep.rolled_back == []

    after = store2.debug_rows()
    after_events = sorted(
        (e.block_number, e.block_hash, e.tx_hash, e.log_index, e.event_name, e.who, e.amount)
        for e in store2.list_events(limit=1000)
    )

    assert before["blocks"] == after["blocks"]
    assert before["balances"] == after["balances"]
    assert before["totals"] == after["totals"]
    assert before_events == after_events
    # and it matches on-chain truth
    onchain_total_dep, onchain_total_wit = d.vault.functions.stats().call()
    assert store2.get_total("deposited") == onchain_total_dep
    assert store2.get_total("withdrawn") == onchain_total_wit
    assert store2.get_balance(d.user1) == d.vault.functions.balanceOf(d.user1).call()

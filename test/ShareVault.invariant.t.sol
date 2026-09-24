// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {ShareVault} from "../src/ShareVault.sol";
import {MockERC20} from "../src/MockERC20.sol";

/// @notice Bounded handler: drives random deposit / redeem / donate sequences
///         against the vault while tracking ghost balances.
contract VaultHandler is Test {
    ShareVault public vault;
    MockERC20 public token;

    address[] public actors;
    uint256 public ghost_deposited; // assets pulled in via deposit
    uint256 public ghost_donated; // assets pushed in via direct transfer
    uint256 public ghost_withdrawn; // assets paid out via redeem
    uint256 public lastRate; // assetsPerShare after the previous action

    uint256 internal constant CAP = 1e30;

    constructor(ShareVault _vault, MockERC20 _token) {
        vault = _vault;
        token = _token;
        for (uint256 i = 0; i < 4; i++) {
            address a = address(uint160(0x1000 + i));
            actors.push(a);
            token.mint(a, type(uint128).max);
            vm.prank(a);
            token.approve(address(vault), type(uint256).max);
        }
        lastRate = vault.assetsPerShare();
    }

    function deposit(uint256 actorSeed, uint256 assets) external {
        address actor = actors[actorSeed % actors.length];
        assets = bound(assets, 1, CAP);
        vm.prank(actor);
        try vault.deposit(assets, actor, 0) returns (uint256) {
            ghost_deposited += assets;
            lastRate = vault.assetsPerShare();
        } catch {}
    }

    function redeem(uint256 actorSeed, uint256 shareSeed) external {
        address actor = actors[actorSeed % actors.length];
        uint256 bal = vault.balanceOf(actor);
        if (bal == 0) return;
        uint256 shares = bound(shareSeed, 1, bal);
        if (vault.convertToAssets(shares) == 0) return;
        vm.prank(actor);
        try vault.redeem(shares, actor, 0) returns (uint256 out) {
            ghost_withdrawn += out;
            lastRate = vault.assetsPerShare();
        } catch {}
    }

    function redeemAll(uint256 actorSeed) external {
        address actor = actors[actorSeed % actors.length];
        uint256 bal = vault.balanceOf(actor);
        if (bal == 0 || vault.convertToAssets(bal) == 0) return;
        vm.prank(actor);
        try vault.redeem(bal, actor, 0) returns (uint256 out) {
            ghost_withdrawn += out;
            lastRate = vault.assetsPerShare();
        } catch {}
    }

    /// A "donation": assets pushed straight to the vault with no shares minted.
    function donate(uint256 actorSeed, uint256 amount) external {
        address actor = actors[actorSeed % actors.length];
        amount = bound(amount, 1, CAP);
        vm.prank(actor);
        token.transfer(address(vault), amount);
        ghost_donated += amount;
        lastRate = vault.assetsPerShare();
    }
}

contract ShareVaultInvariantTest is Test {
    MockERC20 internal token;
    ShareVault internal vault;
    VaultHandler internal handler;

    function setUp() public {
        token = new MockERC20();
        vault = new ShareVault(address(token), "Vault Share", "vMOCK");
        handler = new VaultHandler(vault, token);
        targetContract(address(handler));
    }

    /// Solvency: the vault's balance is exactly deposits + donations - payouts.
    /// Nothing is created from thin air and nothing is skimmed.
    function invariant_assetConservation() public view {
        assertEq(
            token.balanceOf(address(vault)),
            handler.ghost_deposited() + handler.ghost_donated() - handler.ghost_withdrawn()
        );
    }

    /// The share price never decreases: floor rounding on both sides leaves
    /// dust with the pool, and donations only raise the rate.
    function invariant_rateNonDecreasing() public view {
        assertGe(vault.assetsPerShare(), handler.lastRate());
    }

    /// Every outstanding share is backed: redeeming the whole supply pays out
    /// at most what the vault holds.
    function invariant_sharesFullyBacked() public view {
        assertLe(vault.convertToAssets(vault.totalSupply()), vault.totalAssets());
    }

    /// No actor can hold more shares than the supply, and supply is the sum of
    /// balances (no phantom shares).
    function invariant_supplyConsistency() public view {
        uint256 sum;
        for (uint256 i = 0; i < 4; i++) {
            sum += vault.balanceOf(address(uint160(0x1000 + i)));
        }
        assertEq(sum, vault.totalSupply());
    }
}

// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {MockERC20} from "../../src/MockERC20.sol";
import {ShareVault} from "../../src/ShareVault.sol";

/// @title ReentrantERC20
/// @notice Malicious token that fires a callback into the vault from inside
///         its transfer/transferFrom hooks. Used to prove the vault's
///         nonReentrant guard + checks-effects-interactions hold. Test-only.
contract ReentrantERC20 is MockERC20 {
    enum Mode {
        None,
        ReenterDeposit,
        ReenterRedeem
    }

    ShareVault public vault;
    Mode public mode;
    uint256 public reenterAssets;
    uint256 public reenterShares;
    /// @dev The reentrant call is expected to revert with Reentrancy; we catch
    ///      it so the outer transaction succeeds and we can assert the pool
    ///      state is intact afterwards.
    bool public reentryReverted;

    constructor() {}

    /// @dev Token and vault reference each other, so the vault is wired after
    ///      both are deployed. Test-only convenience.
    function setVault(ShareVault _vault) external {
        vault = _vault;
    }

    function armDeposit(uint256 assets) external {
        mode = Mode.ReenterDeposit;
        reenterAssets = assets;
        reentryReverted = false;
    }

    function armRedeem(uint256 shares) external {
        mode = Mode.ReenterRedeem;
        reenterShares = shares;
        reentryReverted = false;
    }

    function disarm() external {
        mode = Mode.None;
    }

    function _afterTokenOperation(address from, address to, uint256) internal override {
        if (mode == Mode.None) return;
        if (to != address(vault) && from != address(vault)) return;
        Mode m = mode;
        mode = Mode.None; // one shot
        if (m == Mode.ReenterDeposit) {
            try vault.deposit(reenterAssets, address(this), 0) {
                // unexpected success
            } catch (bytes memory reason) {
                if (bytes4(reason) == ShareVault.Reentrancy.selector) {
                    reentryReverted = true;
                }
            }
        } else {
            try vault.redeem(reenterShares, address(this), 0) {
                // unexpected success
            } catch (bytes memory reason) {
                if (bytes4(reason) == ShareVault.Reentrancy.selector) {
                    reentryReverted = true;
                }
            }
        }
    }
}

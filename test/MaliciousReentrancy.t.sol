// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {MaliciousERC20} from "./MaliciousERC20.sol";
import {ShareVault} from "../src/ShareVault.sol";

/// @notice 攻击合约：作为存款/赎回主体，被恶意代币在转账回调中驱动发起重入。
contract ReentrancyAttacker {
    MaliciousERC20 public evil;
    ShareVault public vault;

    constructor(MaliciousERC20 evil_, ShareVault vault_) {
        evil = evil_;
        vault = vault_;
    }

    function approveVault(uint256 amount) external {
        evil.approve(address(vault), amount);
    }

    /// @notice 存款攻击：恶意代币在 transferFrom 中回调 reenterDeposit()。
    function attackDeposit(uint256 amount, uint256 reenterAmount) external {
        evil.setHook(
            address(this),
            abi.encodeCall(ReentrancyAttacker.reenterDeposit, (reenterAmount))
        );
        vault.deposit(amount, address(this), 0);
    }

    function reenterDeposit(uint256 amount) external {
        // 重入必须被锁拒绝。用 try/catch 记录是否如预期回滚。
        try vault.deposit(amount, address(this), 0) {
            revert("REENTRANCY_SUCCEEDED");
        } catch (bytes memory reason) {
            // 只接受 Reentrancy 错误；其它错误不应出现。
            bytes4 sel = bytes4(reason);
            require(sel == ShareVault.Reentrancy.selector, "unexpected revert in reentry");
        }
    }

    /// @notice 赎回攻击：恶意代币在 transfer 中回调 reenterRedeem()。
    function attackRedeem(uint256 depositFirst, uint256 redeemShares, uint256 reenterShares)
        external
    {
        // 先正常建立一笔份额（首存金额足够大）。
        vault.deposit(depositFirst, address(this), 0);
        evil.setHook(
            address(this),
            abi.encodeCall(ReentrancyAttacker.reenterRedeem, (reenterShares))
        );
        vault.redeem(redeemShares, address(this), address(this), 0);
    }

    function reenterRedeem(uint256 shares) external {
        try vault.redeem(shares, address(this), address(this), 0) {
            revert("REENTRANCY_SUCCEEDED");
        } catch (bytes memory reason) {
            bytes4 sel = bytes4(reason);
            require(sel == ShareVault.Reentrancy.selector, "unexpected revert in reentry");
        }
    }
}

contract MaliciousCallbackTest is Test {
    MaliciousERC20 evil;
    ShareVault vault;
    ReentrancyAttacker attacker;

    function setUp() public {
        evil = new MaliciousERC20(20_000 ether);
        vault = new ShareVault(address(evil), "Evil Vault", "sEVIL");
        attacker = new ReentrancyAttacker(evil, vault);
    }

    function _seed(address to, uint256 amount) internal {
        // 部署时发行量在测试合约上，直接转给攻击合约。
        evil.transfer(to, amount);
    }

    /// @dev 存款转账回调中重入 deposit：必须被重入锁拒绝，最终只铸一笔份额。
    function test_deposit_reentrancyBlocked() public {
        uint256 amount = 10_000 ether;
        _seed(address(attacker), amount);
        attacker.approveVault(type(uint256).max);

        uint256 supplyBefore = vault.totalSupply();
        attacker.attackDeposit(amount, 1_000 ether);

        uint256 attackerShares = vault.balanceOf(address(attacker));
        // 首存：shares = amount - 1000（死份额），没有因重入多发。
        assertEq(attackerShares, amount - 1000, "exactly one deposit minted");
        assertEq(vault.totalSupply(), supplyBefore + amount, "no double mint");
        assertEq(evil.balanceOf(address(vault)), amount, "single asset transfer");
    }

    /// @dev 赎回转账回调中重入 redeem：必须被拒绝，份额只销毁一次。
    function test_redeem_reentrancyBlocked() public {
        uint256 amount = 10_000 ether;
        _seed(address(attacker), amount);
        attacker.approveVault(type(uint256).max);

        uint256 shares = amount - 1000;
        uint256 supplyBefore = vault.totalSupply(); // 0
        attacker.attackRedeem(amount, shares / 2, shares);

        // 外层赎回销毁一半；重入的那次完全没有生效。
        assertEq(vault.balanceOf(address(attacker)), shares - shares / 2, "only outer redeem burned");
        assertEq(vault.totalSupply(), supplyBefore + amount - shares / 2, "supply consistent");
    }
}

// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {ERC20} from "../ERC20.sol";
import {CPMMPair} from "../CPMMPair.sol";

/// @notice Malicious ERC20: on transfers into the target pair it reenters
///         one of the pair's locked entry points. Every reentry must revert
///         with CPMMPair.Locked().
contract ReentrantERC20 is ERC20 {
    address public targetPair;
    uint8 public attackMode; // 0 = swap, 1 = mint, 2 = burn, 3 = skim, 4 = sync
    bool public attacking = true;
    uint256 private _depth;

    constructor() ERC20("Reentrant Token", "REENT") {}

    function configure(address pair, uint8 mode) external {
        targetPair = pair;
        attackMode = mode;
    }

    function setAttacking(bool v) external {
        attacking = v;
    }

    function mint(address to, uint256 value) external {
        _mint(to, value);
    }

    function _transfer(address from, address to, uint256 value) internal override {
        require(balanceOf[from] >= value, "ERC20: balance");
        unchecked {
            balanceOf[from] -= value;
        }
        balanceOf[to] += value;
        emit Transfer(from, to, value);

        if (attacking && to == targetPair && _depth == 0) {
            _depth = 1;
            CPMMPair p = CPMMPair(targetPair);
            uint8 mode = attackMode;
            if (mode == 0) {
                // Try to enter swap again while the outer swap is locked.
                try p.swap(0, 0, address(this), 0, 0, "") {} catch {}
            } else if (mode == 1) {
                try p.mint(from, 0, 0) {} catch {}
            } else if (mode == 2) {
                try p.burn(from) {} catch {}
            } else if (mode == 3) {
                try p.skim(from) {} catch {}
            } else {
                try p.sync() {} catch {}
            }
            _depth = 0;
        }
    }
}

// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "../interfaces/IERC20.sol";

/// @title OutgoingFeeToken
/// @notice Directional fee-on-transfer token used to exercise the pool's
///         active probe. Transfers INTO the pool credit the pool the full
///         nominal amount (so the pool's incoming balance-delta check
///         passes), but transfers OUT OF the pool credit the recipient
///         less than nominal — exactly the recipient-side fee that simple
///         balance-delta accounting cannot see.
///         The BalanceProbe is exempt as a sender (it relays tokens back),
///         so the FIRST probe leg (pool -> probe) measures the fee and the
///         pool rejects the token before anything else happens.
contract OutgoingFeeToken is IERC20 {
    string public name;
    string public symbol;
    uint8 public constant decimals = 18;

    uint256 public immutable feeBps;
    address public immutable victim; // the sender whose outgoing transfers fee

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    constructor(string memory _name, string memory _symbol, uint256 _feeBps, address _victim) {
        name = _name;
        symbol = _symbol;
        feeBps = _feeBps;
        victim = _victim;
    }

    function mint(address to, uint256 amount) external {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transfer(address to, uint256 value) external returns (bool) {
        _transfer(msg.sender, to, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        if (allowed != type(uint256).max) {
            require(allowed >= value, "OFT: allowance");
            unchecked {
                allowance[from][msg.sender] = allowed - value;
            }
        }
        _transfer(from, to, value);
        return true;
    }

    function _transfer(address from, address to, uint256 value) private {
        require(balanceOf[from] >= value, "OFT: balance");
        uint256 fee = (from == victim) ? (value * feeBps) / 10_000 : 0;
        unchecked {
            balanceOf[from] -= value;
            balanceOf[to] += value - fee;
            totalSupply -= fee;
        }
        emit Transfer(from, to, value - fee);
    }
}

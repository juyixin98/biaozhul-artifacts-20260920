// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title SyntheticToken
/// @notice Minimal, dependency-free ERC-20 used as the synthetic asset for the
///         vesting escrow. The deployer receives an optional initial supply and
///         may mint freely (test/local-chain use only).
contract SyntheticToken {
    string public name;
    string public symbol;
    uint8 public constant decimals = 18;

    uint256 public totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    address public minter;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    error InsufficientBalance();
    error InsufficientAllowance();
    error InvalidReceiver();

    constructor(string memory _name, string memory _symbol, uint256 initialSupply) {
        name = _name;
        symbol = _symbol;
        minter = msg.sender;
        if (initialSupply != 0) {
            _mint(msg.sender, initialSupply);
        }
    }

    modifier onlyMinter() {
        require(msg.sender == minter, "not minter");
        _;
    }

    function mint(address to, uint256 amount) external onlyMinter {
        _mint(to, amount);
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _transfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        if (allowed != type(uint256).max) {
            if (allowed < amount) revert InsufficientAllowance();
            unchecked {
                allowance[from][msg.sender] = allowed - amount;
            }
        }
        _transfer(from, to, amount);
        return true;
    }

    function _transfer(address from, address to, uint256 amount) internal {
        if (to == address(0)) revert InvalidReceiver();
        uint256 fromBalance = balanceOf[from];
        if (fromBalance < amount) revert InsufficientBalance();
        unchecked {
            balanceOf[from] = fromBalance - amount;
            balanceOf[to] += amount;
        }
        emit Transfer(from, to, amount);
    }

    function _mint(address to, uint256 amount) internal {
        if (to == address(0)) revert InvalidReceiver();
        totalSupply += amount; // checked: reverts on overflow
        unchecked {
            balanceOf[to] += amount;
        }
        emit Transfer(address(0), to, amount);
    }
}

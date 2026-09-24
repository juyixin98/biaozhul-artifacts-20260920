// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC1967Upgrade} from "../proxy/ERC1967Upgrade.sol";

/// @title UUPSUpgradable — base logic contract carrying the upgrade admin.
/// @notice The owner field is REAL application storage: it occupies slot 0 of
///         every logic version and is shared through the proxy. Logic versions
///         MUST keep this layout (the Python checker enforces it on the full
///         combined layout JSON), and MUST keep the storage gap at the tail of
///         the base so future base fields can be added safely.
abstract contract UUPSUpgradable is ERC1967Upgrade {
    address internal _owner;

    event OwnershipTransferred(
        address indexed previousOwner, address indexed newOwner
    );

    modifier onlyOwner() {
        require(msg.sender == _owner, "UUPS: not owner");
        _;
    }

    /// @dev Called through the proxy's delegatecall, so msg.sender is the
    ///      account that sends the deployment/init transaction.
    function __UUPS_init() internal {
        require(_owner == address(0), "UUPS: already initialized");
        _owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    function owner() external view returns (address) {
        return _owner;
    }

    /// @notice UUPS entry point — called through the proxy, so `_owner` is read
    ///         from the proxy's storage. Restricted to the current owner.
    function upgradeTo(address newImplementation) external onlyOwner {
        _setImplementation(newImplementation);
    }

    uint256[49] private __gap;
}

// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title UpgradeableProxy
/// @notice Minimal EIP-1967 transparent-style proxy. The admin (set at
///         construction) may swap the implementation; all other calls are
///         delegated to the current implementation. Storage slots for
///         implementation/admin live in the EIP-1967 slots so they never
///         collide with implementation storage.
contract UpgradeableProxy {
    /// @dev bytes32(uint256(keccak256("eip1967.proxy.implementation")) - 1)
    bytes32 private constant _IMPLEMENTATION_SLOT =
        0x360894a13ba1a3210667c828361db98de5b499d3a26d4b4a8b7f4e6d4b2e9c01;
    /// @dev bytes32(uint256(keccak256("eip1967.proxy.admin")) - 1)
    bytes32 private constant _ADMIN_SLOT =
        0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103;

    event Upgraded(address indexed implementation);

    constructor(address initialImplementation, address admin) {
        _setAdmin(admin);
        _upgradeTo(initialImplementation);
    }

    function implementation() public view returns (address impl) {
        assembly {
            impl := sload(_IMPLEMENTATION_SLOT)
        }
    }

    function admin() public view returns (address adm) {
        assembly {
            adm := sload(_ADMIN_SLOT)
        }
    }

    /// @notice Upgrade the implementation. Only the admin may call this.
    ///         Callers are expected to have validated storage-layout
    ///         compatibility off-chain (e.g. with this repo's checker).
    function upgradeTo(address newImplementation) external {
        require(msg.sender == admin(), "UpgradeableProxy: not admin");
        _upgradeTo(newImplementation);
    }

    function _upgradeTo(address newImplementation) internal {
        require(newImplementation.code.length > 0, "UpgradeableProxy: not a contract");
        assembly {
            sstore(_IMPLEMENTATION_SLOT, newImplementation)
        }
        emit Upgraded(newImplementation);
    }

    function _setAdmin(address newAdmin) internal {
        require(newAdmin != address(0), "UpgradeableProxy: zero admin");
        assembly {
            sstore(_ADMIN_SLOT, newAdmin)
        }
    }

    fallback() external payable {
        address impl = implementation();
        assembly {
            calldatacopy(0, 0, calldatasize())
            let result := delegatecall(gas(), impl, 0, calldatasize(), 0, 0)
            returndatacopy(0, 0, returndatasize())
            switch result
            case 0 { revert(0, returndatasize()) }
            default { return(0, returndatasize()) }
        }
    }

    receive() external payable {}
}

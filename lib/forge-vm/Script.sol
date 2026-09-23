// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Vm} from "./Vm.sol";
import {console} from "./console.sol";

/// @title Script
/// @notice Minimal script base contract (local substitute for
///         forge-std/Script.sol). `forge script` executes `run()`.
abstract contract Script {
    bool public IS_SCRIPT = true;
    Vm internal constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);
}

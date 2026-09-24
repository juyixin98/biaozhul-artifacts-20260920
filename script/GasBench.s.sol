// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Checkpoints} from "../src/Checkpoints.sol";

interface Vm {
    function roll(uint256 newHeight) external;
}

/// @notice Standalone gas benchmark, prints one GasTable row per measurement.
contract GasBench {
    Vm internal constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

    event GasTable(uint256 checkpointCount, string op, uint256 gasUsed);

    function run() external {
        writeBench();
        readBench();
    }

    function writeBench() internal {
        Checkpoints c = new Checkpoints();
        vm.roll(1);

        uint256 g = _set(c, 7);
        emit GasTable(0, "append_first", g);

        vm.roll(2);
        uint256 appendAt2 = _set(c, 8);
        emit GasTable(1, "append", appendAt2);

        uint256 merged = _set(c, 9);
        emit GasTable(2, "merge_same_block", merged);

        for (uint256 b = 3; b <= 200; b++) {
            vm.roll(b);
            c.setValue(b);
        }
        uint256 appendAt200 = _set(c, 200);
        emit GasTable(200, "append", appendAt200);
        uint256 mergedAt200 = _set(c, 201);
        emit GasTable(200, "merge_same_block", mergedAt200);
    }

    function readBench() internal {
        uint256[5] memory sizes = [uint256(4), 16, 64, 256, 1024];
        for (uint256 k = 0; k < sizes.length; k++) {
            Checkpoints c = new Checkpoints();
            for (uint256 b = 1; b <= sizes[k]; b++) {
                vm.roll(b);
                c.setValue(b * 3);
            }
            uint256 beforeGas = gasleft();
            c.getAtBlock(sizes[k]);
            uint256 used = beforeGas - gasleft();
            emit GasTable(sizes[k], "lookup_latest_cold", used);
        }
    }

    function _set(Checkpoints c, uint256 value)
        internal
        returns (uint256 used)
    {
        uint256 beforeGas = gasleft();
        c.setValue(value);
        used = beforeGas - gasleft();
    }
}

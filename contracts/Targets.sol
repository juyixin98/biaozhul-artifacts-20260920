// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice Test targets for MultisigTimelock.

/// @dev Counter with an execution-callback hook: the executor is recorded and a
///      dedicated flag marks that the call came from the timelock contract. Also
///      counts calls, which lets tests prove at-most-once execution.
contract Counter {
    uint256 public count;
    address public lastCaller;
    bytes32 public lastDataHash;
    uint256 public callCount;

    event Incremented(address indexed caller, uint256 newCount, bytes32 dataHash);

    function increment(uint256 step, bytes32 tag) external returns (uint256) {
        count += step;
        callCount++;
        lastCaller = msg.sender;
        lastDataHash = keccak256(abi.encode(step, tag));
        emit Incremented(msg.sender, count, lastDataHash);
        return count;
    }
}

/// @dev Fails while `failing` is true, then succeeds permanently once an
///      operator flips the switch. (A target cannot count its own failed calls:
///      a reverting call rolls the counter back. The external switch models the
///      transient failure being repaired between attempts.)
contract FlakyTarget {
    bool public failing = true;
    uint256 public attempts;
    uint256 public successes;

    event FlakyAttempt(uint256 attempt, bool success);

    function setFailing(bool v) external {
        failing = v;
    }

    function run() external returns (bool) {
        attempts++;
        if (failing) {
            emit FlakyAttempt(attempts, false);
            revert("FlakyTarget: simulated failure");
        }
        successes++;
        emit FlakyAttempt(attempts, true);
        return true;
    }
}

/// @dev Always reverts - used to prove retries are eventually exhausted and the
///      op becomes permanently Failed.
contract AlwaysFail {
    function boom() external pure {
        revert("AlwaysFail: never succeeds");
    }
}

interface IMultisigTimelock {
    function execute(
        address target,
        uint256 value,
        bytes calldata data,
        uint64 opNonce,
        uint64 deadline
    ) external returns (bytes memory);
}

/// @dev Tries to re-enter the executor on first call using the very same
///      operation payload (its own msg.data). The nonReentrant guard must block
///      the nested execute(); the nested revert is swallowed so the outer call
///      still succeeds exactly once.
contract ReentrantTarget {
    IMultisigTimelock public immutable executor;
    uint256 public successes;
    bool private _entered;

    constructor(address executor_) {
        executor = IMultisigTimelock(executor_);
    }

    function callback(uint64 opNonce, uint64 deadline) external {
        if (!_entered) {
            _entered = true;
            try executor.execute(address(this), 0, msg.data, opNonce, deadline) {} catch {
                // expected: nonReentrant guard rejects the nested execution
            }
            _entered = false;
        }
        successes++;
    }
}


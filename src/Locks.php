<?php

declare(strict_types=1);

namespace TravelOps;

use Illuminate\Database\Capsule\Manager as Capsule;
use Throwable;

/**
 * Serialization for races that row locks alone cannot express — in particular
 * import vs daily close, where the two transactions may not touch a common
 * parent row.
 *
 * A named MySQL advisory lock is acquired OUTSIDE/AROUND the transaction and
 * only released AFTER commit (or rollback). This ordering matters: releasing
 * the lock before commit would let a contender observe a state where the
 * holder's writes are still invisible, allowing a close to miss an in-flight
 * import. (MySQL guarantees GET_LOCK survives across transactions; it is
 * session-scoped, so we release it explicitly in finally.)
 */
final class Locks
{
    /**
     * Acquire every named lock (in the given order), run a transaction, then
     * release the locks only after the transaction finished.
     *
     * @template T
     * @param list<string> $lockNames
     * @param callable():T $fn
     * @return T
     */
    public static function withLocks(array $lockNames, callable $fn, int $timeout = 10): mixed
    {
        $conn = Capsule::connection();
        $held = [];

        try {
            foreach ($lockNames as $name) {
                $got = (int) $conn->selectOne('SELECT GET_LOCK(?, ?) AS r', [$name, $timeout])->r;
                if ($got !== 1) {
                    throw ApiException::conflict('resource is busy, retry', 'lock_busy', ['lock' => $name]);
                }
                $held[] = $name;
            }

            // Commit happens INSIDE the lock window, so contenders only see
            // the post-commit state.
            return $conn->transaction($fn);
        } finally {
            // Release after commit/rollback, in reverse acquisition order.
            foreach (array_reverse($held) as $name) {
                try {
                    $conn->statement('SELECT RELEASE_LOCK(?)', [$name]);
                } catch (Throwable) {
                    // connection gone; MySQL releases session locks automatically
                }
            }
        }
    }

    /**
     * @template T
     * @param callable():T $fn
     * @return T
     */
    public static function withTransactionalLock(string $namespace, string $key, callable $fn, int $timeout = 10): mixed
    {
        return self::withLocks([substr($namespace . ':' . $key, 0, 64)], $fn, $timeout);
    }
}

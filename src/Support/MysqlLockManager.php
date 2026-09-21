<?php

declare(strict_types=1);

namespace Meridian\Support;

use Illuminate\Database\Capsule\Manager as DB;
use Meridian\DomainException;

/**
 * MySQL named locks (GET_LOCK). Used to serialise daily-close against
 * imports/settlements touching the same business date, so a close and an
 * import can never interleave into a half-closed day.
 */
final class MysqlLockManager implements LockManager
{
    public function acquire(string $name): void
    {
        $row = DB::select('SELECT GET_LOCK(?, 15) AS got', [$name]);
        if ((int) ($row[0]->got ?? 0) !== 1) {
            throw new DomainException("could not acquire lock '{$name}'", 503, 'lock_timeout');
        }
    }

    public function release(string $name): void
    {
        DB::select('SELECT RELEASE_LOCK(?) AS freed', [$name]);
    }
}

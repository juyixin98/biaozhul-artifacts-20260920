<?php

declare(strict_types=1);

namespace Meridian\Support;

/**
 * No-op lock for SQLite (single-writer engine; tests run single-process).
 */
final class NullLockManager implements LockManager
{
    public function acquire(string $name): void
    {
    }

    public function release(string $name): void
    {
    }
}

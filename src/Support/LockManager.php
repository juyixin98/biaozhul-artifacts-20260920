<?php

declare(strict_types=1);

namespace Meridian\Support;

interface LockManager
{
    /** Acquire a named mutual-exclusion lock; blocks up to an internal timeout. */
    public function acquire(string $name): void;

    public function release(string $name): void;
}

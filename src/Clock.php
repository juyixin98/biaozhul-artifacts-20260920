<?php

declare(strict_types=1);

namespace TravelOps;

use DateTimeImmutable;
use DateTimeZone;

/**
 * Single source of "now" (UTC). Tests may freeze the clock.
 */
final class Clock
{
    private static ?DateTimeImmutable $frozen = null;

    public static function freeze(DateTimeImmutable $at): void
    {
        self::$frozen = $at->setTimezone(new DateTimeZone('UTC'));
    }

    public static function reset(): void
    {
        self::$frozen = null;
    }

    public static function now(): DateTimeImmutable
    {
        return self::$frozen ?? new DateTimeImmutable('now', new DateTimeZone('UTC'));
    }
}

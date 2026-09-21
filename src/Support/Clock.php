<?php

declare(strict_types=1);

namespace Meridian\Support;

/**
 * Process-wide clock. Tests may pin the time with Clock::setFixed().
 */
final class Clock
{
    private static ?\DateTimeImmutable $fixed = null;

    public static function now(): \DateTimeImmutable
    {
        return self::$fixed ?? new \DateTimeImmutable('now');
    }

    public static function setFixed(?\DateTimeImmutable $now): void
    {
        self::$fixed = $now;
    }
}

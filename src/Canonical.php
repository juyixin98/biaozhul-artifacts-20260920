<?php

declare(strict_types=1);

namespace TravelOps;

/**
 * Deterministic canonical JSON (sorted keys, no unicode escaping) used to
 * build content hashes so the same logical content always hashes identically.
 */
final class Canonical
{
    public static function json(mixed $value): string
    {
        return json_encode(
            self::sort($value),
            JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRESERVE_ZERO_FRACTION
        );
    }

    public static function sha256(mixed $value): string
    {
        return hash('sha256', self::json($value));
    }

    private static function sort(mixed $value): mixed
    {
        if (is_array($value)) {
            $isList = array_is_list($value);
            $value = array_map(self::sort(...), $value);
            if (!$isList) {
                ksort($value, SORT_STRING);
            }
            return $value;
        }
        return $value;
    }
}

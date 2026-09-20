<?php

declare(strict_types=1);

namespace TravelOps;

final class Env
{
    public static function get(string $key, ?string $default = null): ?string
    {
        $value = $_SERVER[$key] ?? $_ENV[$key] ?? getenv($key);
        if ($value === false || $value === null || $value === '') {
            return $default;
        }
        return (string) $value;
    }
}

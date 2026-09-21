<?php

declare(strict_types=1);

namespace Meridian\Support;

use Illuminate\Database\Capsule\Manager as Capsule;

final class Database
{
    private static ?Capsule $capsule = null;

    public static function init(): Capsule
    {
        $capsule = new Capsule();
        $driver = getenv('DB_DRIVER') ?: 'sqlite';

        if ($driver === 'mysql') {
            $capsule->addConnection([
                'driver' => 'mysql',
                'host' => getenv('DB_HOST') ?: '127.0.0.1',
                'port' => (int) (getenv('DB_PORT') ?: 3306),
                'database' => getenv('DB_DATABASE') ?: 'meridian',
                'username' => getenv('DB_USERNAME') ?: 'root',
                'password' => getenv('DB_PASSWORD') ?: '',
                'charset' => 'utf8mb4',
                'collation' => 'utf8mb4_unicode_ci',
                'prefix' => '',
            ]);
        } else {
            $capsule->addConnection([
                'driver' => 'sqlite',
                'database' => getenv('DB_DATABASE') ?: ':memory:',
                'prefix' => '',
                'foreign_key_constraints' => false,
            ]);
        }

        $capsule->setAsGlobal();
        $capsule->bootEloquent();
        self::$capsule = $capsule;

        return $capsule;
    }

    public static function capsule(): Capsule
    {
        if (self::$capsule === null) {
            self::init();
        }
        return self::$capsule;
    }

    /**
     * Wait until the database accepts connections (container startup races).
     */
    public static function waitForDb(int $attempts = 30): void
    {
        $capsule = self::capsule();
        $last = null;
        for ($i = 0; $i < $attempts; $i++) {
            try {
                $capsule->getConnection()->getPdo();
                return;
            } catch (\Throwable $e) {
                $last = $e;
                sleep(1);
            }
        }
        throw new \RuntimeException('Database not reachable: ' . ($last?->getMessage() ?? 'unknown'));
    }

    /**
     * Drop the current connection (used after pcntl_fork so parent and child
     * never share a PDO handle).
     */
    public static function reconnect(): Capsule
    {
        self::$capsule = null;
        return self::init();
    }
}

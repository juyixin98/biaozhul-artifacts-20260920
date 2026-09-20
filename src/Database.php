<?php

declare(strict_types=1);

namespace TravelOps;

use Illuminate\Container\Container as IllumContainer;
use Illuminate\Database\Capsule\Manager as Capsule;
use Illuminate\Events\Dispatcher;

/** Boots Eloquent against the configured MySQL database. */
final class Database
{
    private static bool $booted = false;

    public static function boot(?array $config = null): Capsule
    {
        if (self::$booted) {
            return Capsule::connection()->getCapsule();
        }

        $config ??= [
            'driver' => 'mysql',
            'host' => Env::get('DB_HOST', '127.0.0.1'),
            'port' => (int) Env::get('DB_PORT', '3306'),
            'database' => Env::get('DB_NAME', 'travelops'),
            'username' => Env::get('DB_USER', 'root'),
            'password' => Env::get('DB_PASSWORD', 'travelops'),
            'charset' => 'utf8mb4',
            'collation' => 'utf8mb4_unicode_ci',
            'prefix' => '',
            'timezone' => '+00:00',
            'options' => [
                \PDO::ATTR_EMULATE_PREPARES => false,
            ],
        ];

        $capsule = new Capsule();
        $capsule->addConnection($config);
        $capsule->setEventDispatcher(new Dispatcher(new IllumContainer()));
        $capsule->setAsGlobal();
        $capsule->bootEloquent();

        // Fixed-point safety: decimals come back as strings.
        Capsule::connection()->getPdo()->setAttribute(\PDO::ATTR_EMULATE_PREPARES, false);

        self::$booted = true;
        return $capsule;
    }

    /** Drop/recreate test state (used by the test suite only). */
    public static function reset(): void
    {
        $db = Env::get('DB_NAME', 'travelops');
        $conn = Capsule::connection();
        $rows = $conn->select(
            'SELECT table_name FROM information_schema.tables WHERE table_schema = ?',
            [$db]
        );
        $conn->statement('SET FOREIGN_KEY_CHECKS = 0');
        foreach ($rows as $row) {
            $name = array_values((array) $row)[0];
            $conn->statement('DROP TABLE IF EXISTS `' . $name . '`');
        }
        $conn->statement('SET FOREIGN_KEY_CHECKS = 1');
    }
}

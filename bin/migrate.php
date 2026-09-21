<?php

declare(strict_types=1);

use Meridian\Support\Database;
use Meridian\Support\Migrator;

require __DIR__ . '/../vendor/autoload.php';

Database::init();
Database::waitForDb();

$ran = (new Migrator(__DIR__ . '/../database/migrations'))->run();

if ($ran === []) {
    echo "migrations: nothing to apply\n";
} else {
    foreach ($ran as $name) {
        echo "migrated: {$name}\n";
    }
}

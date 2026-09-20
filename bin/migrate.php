<?php

declare(strict_types=1);

require __DIR__ . '/../vendor/autoload.php';

use TravelOps\Database;
use TravelOps\Env;

Database::boot();

$schema = file_get_contents(__DIR__ . '/../database/schema.sql');
if ($schema === false) {
    fwrite(STDERR, "cannot read schema.sql\n");
    exit(1);
}

// Drop `--` comment lines, then split statements on the trailing semicolon.
$lines = [];
foreach (preg_split('/\r?\n/', $schema) as $line) {
    if (preg_match('/^\s*--/', $line)) {
        continue;
    }
    $lines[] = $line;
}
$cleaned = implode("\n", $lines);

$conn = Illuminate\Database\Capsule\Manager::connection();
$count = 0;
foreach (array_filter(array_map('trim', explode(";", $cleaned))) as $statement) {
    if ($statement === '') {
        continue;
    }
    $conn->statement($statement);
    $count++;
}
echo "[migrate] {$count} statements applied to " . Env::get('DB_NAME', 'travelops') . "\n";

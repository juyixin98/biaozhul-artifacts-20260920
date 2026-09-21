<?php

declare(strict_types=1);

require __DIR__ . '/../vendor/autoload.php';

// Default to in-memory SQLite for unit runs; set DB_DRIVER=mysql (plus
// DB_HOST/...) to run the full suite, including fork-based race tests.
if (!getenv('DB_DRIVER')) {
    putenv('DB_DRIVER=sqlite');
    putenv('DB_DATABASE=:memory:');
}

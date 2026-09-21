FROM php:8.2-cli

RUN apt-get update && apt-get install -y --no-install-recommends unzip \
    && rm -rf /var/lib/apt/lists/* \
    && docker-php-ext-install pdo_mysql bcmath pcntl

COPY --from=composer:2 /usr/bin/composer /usr/bin/composer

WORKDIR /app

COPY composer.json ./
RUN composer install --no-interaction --prefer-dist --no-progress

COPY . .
RUN composer dump-autoload

EXPOSE 8080

CMD ["sh", "-c", "php bin/migrate.php && php bin/seed.php && php -S 0.0.0.0:8080 -t public"]

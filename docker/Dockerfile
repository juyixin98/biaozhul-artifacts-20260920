FROM composer:2.8 AS vendor
WORKDIR /app
COPY composer.json ./
RUN composer update --no-interaction --prefer-dist --no-scripts --optimize-autoloader \
    --ignore-platform-req=ext-bcmath

FROM php:8.2-cli
RUN apt-get update && apt-get install -y --no-install-recommends \
        libzip-dev unzip git \
    && docker-php-ext-install pdo_mysql bcmath \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /var/www
COPY --from=vendor /app/vendor ./vendor
COPY . .

RUN chmod +x docker/entrypoint.sh bin/*.sh 2>/dev/null || true

EXPOSE 8080
ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["serve"]

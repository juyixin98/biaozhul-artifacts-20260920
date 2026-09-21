<?php

use Illuminate\Database\Capsule\Manager as DB;

return new class {
    public function up(): void
    {
        DB::schema()->create('import_batches', function ($table) {
            $table->id();
            $table->string('batch_no')->unique();
            $table->string('source');
            $table->string('status')->default('committed'); // a batch row only exists if fully committed
            $table->unsignedInteger('inserted_count')->default(0);
            $table->unsignedInteger('duplicate_count')->default(0);
            $table->string('created_by');
            $table->timestamp('created_at')->useCurrent();
        });

        DB::schema()->create('settlement_lines', function ($table) {
            $table->id();
            $table->unsignedBigInteger('batch_id');
            $table->unsignedBigInteger('contract_id');
            $table->char('currency', 3);
            $table->date('business_date');
            $table->string('external_ref')->unique(); // external流水ID: dedup key
            $table->bigInteger('amount_minor');       // fixed-point minor units
            $table->string('description')->nullable();
            $table->char('content_hash', 64);         // detects same-ref-different-content conflicts
            $table->string('status')->default('active'); // active | reversed
            $table->timestamps();
            $table->index(['contract_id', 'currency', 'business_date'], 'lines_agg_idx');
            $table->foreign('batch_id')->references('id')->on('import_batches');
            $table->foreign('contract_id')->references('id')->on('contracts');
        });

        DB::schema()->create('allocation_rule_versions', function ($table) {
            $table->id();
            $table->string('name');
            $table->unsignedInteger('version_no');
            // [{target: string, weight: int>0}, ...]
            $table->json('rules');
            $table->string('remainder_target')->nullable(); // explicit rounding-remainder owner
            $table->string('created_by');
            $table->timestamp('created_at')->useCurrent();
            $table->unique(['name', 'version_no']);
        });

        DB::schema()->create('settlements', function ($table) {
            $table->id();
            $table->unsignedBigInteger('contract_id');
            $table->unsignedBigInteger('contract_version_id'); // must be a fully signed version
            $table->char('currency', 3);
            $table->date('business_date');
            $table->unsignedBigInteger('rule_version_id');
            $table->bigInteger('total_minor');
            $table->string('status')->default('final');
            $table->string('created_by');
            $table->timestamp('created_at')->useCurrent();
            // idempotent retry of the same settlement request -> 409, not a duplicate
            $table->unique(
                ['contract_version_id', 'currency', 'business_date', 'rule_version_id'],
                'settlement_dedup_idx'
            );
            $table->foreign('contract_id')->references('id')->on('contracts');
            $table->foreign('contract_version_id')->references('id')->on('contract_versions');
            $table->foreign('rule_version_id')->references('id')->on('allocation_rule_versions');
        });

        DB::schema()->create('settlement_allocations', function ($table) {
            $table->id();
            $table->unsignedBigInteger('settlement_id');
            $table->string('target');
            $table->unsignedInteger('weight');
            $table->bigInteger('amount_minor');
            $table->boolean('is_remainder_sink')->default(false); // owns the rounding remainder
            $table->timestamp('created_at')->useCurrent();
            $table->foreign('settlement_id')->references('id')->on('settlements');
        });
    }

    public function down(): void
    {
        DB::schema()->dropIfExists('settlement_allocations');
        DB::schema()->dropIfExists('settlements');
        DB::schema()->dropIfExists('allocation_rule_versions');
        DB::schema()->dropIfExists('settlement_lines');
        DB::schema()->dropIfExists('import_batches');
    }
};

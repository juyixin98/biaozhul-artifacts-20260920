<?php

use Illuminate\Database\Capsule\Manager as DB;

return new class {
    public function up(): void
    {
        DB::schema()->create('daily_closes', function ($table) {
            $table->id();
            $table->date('close_date')->unique();
            $table->string('closed_by');
            $table->timestamp('created_at')->useCurrent();
        });

        // Corrections for closed days: never mutate the original record,
        // post a reversal/adjustment against a later, still-open business date.
        DB::schema()->create('adjustments', function ($table) {
            $table->id();
            $table->string('target_type');          // line | settlement
            $table->unsignedBigInteger('target_id');
            $table->string('type');                 // reversal | adjustment
            $table->bigInteger('amount_minor');     // reversal: negative of original
            $table->date('business_date');          // must be an open day after the original date
            $table->string('reason');
            $table->string('operator');
            $table->timestamp('created_at')->useCurrent();
            $table->index(['target_type', 'target_id']);
        });
    }

    public function down(): void
    {
        DB::schema()->dropIfExists('adjustments');
        DB::schema()->dropIfExists('daily_closes');
    }
};

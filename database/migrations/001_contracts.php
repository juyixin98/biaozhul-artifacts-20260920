<?php

use Illuminate\Database\Capsule\Manager as DB;

return new class {
    public function up(): void
    {
        DB::schema()->create('contract_templates', function ($table) {
            $table->id();
            $table->string('name');
            $table->mediumText('body'); // contains {{variable}} placeholders
            $table->timestamps();
        });

        DB::schema()->create('contracts', function ($table) {
            $table->id();
            $table->string('code')->unique();
            $table->unsignedBigInteger('template_id');
            $table->string('title');
            // draft -> signing -> signed | withdrawn | expired
            $table->string('status')->default('draft');
            $table->string('created_by');
            $table->timestamps();
            $table->foreign('template_id')->references('id')->on('contract_templates');
        });

        DB::schema()->create('contract_versions', function ($table) {
            $table->id();
            $table->unsignedBigInteger('contract_id');
            $table->unsignedInteger('version_no');
            $table->json('variables');
            $table->mediumText('content');           // rendered, frozen at sign initiation
            $table->char('content_hash', 64);        // sha256 digest of frozen content
            // draft -> signing -> signed | superseded | withdrawn | expired
            $table->string('status')->default('draft');
            $table->timestamp('sign_initiated_at')->nullable();
            $table->timestamp('expires_at')->nullable();
            $table->timestamps();
            $table->unique(['contract_id', 'version_no']);
            $table->foreign('contract_id')->references('id')->on('contracts');
        });

        DB::schema()->create('contract_signers', function ($table) {
            $table->id();
            $table->unsignedBigInteger('version_id');
            $table->unsignedInteger('seq');          // 1-based signing order
            $table->string('signer_name');
            $table->string('signer_role')->nullable();
            $table->string('status')->default('pending'); // pending | confirmed
            $table->timestamp('confirmed_at')->nullable();
            $table->string('confirmed_by')->nullable();   // operator who performed the confirmation
            $table->timestamps();
            $table->unique(['version_id', 'seq']);
            $table->foreign('version_id')->references('id')->on('contract_versions');
        });

        DB::schema()->create('contract_events', function ($table) {
            $table->id();
            $table->unsignedBigInteger('contract_id');
            $table->unsignedBigInteger('version_id')->nullable();
            $table->string('action');   // contract_created, version_created, version_superseded,
                                        // sign_initiated, confirmed, signed, withdrawn, expired
            $table->string('operator');
            $table->json('detail')->nullable();
            $table->timestamp('created_at')->useCurrent();
            $table->index(['contract_id', 'id']);
        });
    }

    public function down(): void
    {
        DB::schema()->dropIfExists('contract_events');
        DB::schema()->dropIfExists('contract_signers');
        DB::schema()->dropIfExists('contract_versions');
        DB::schema()->dropIfExists('contracts');
        DB::schema()->dropIfExists('contract_templates');
    }
};

<?php

declare(strict_types=1);

namespace Meridian\Models;

use Illuminate\Database\Eloquent\Model;

class ImportBatch extends Model
{
    protected $table = 'import_batches';
    public const UPDATED_AT = null;
    protected $fillable = ['batch_no', 'source', 'status', 'inserted_count', 'duplicate_count', 'created_by'];

    public function lines()
    {
        return $this->hasMany(SettlementLine::class, 'batch_id');
    }
}

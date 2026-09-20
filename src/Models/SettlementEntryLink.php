<?php

declare(strict_types=1);

namespace TravelOps\Models;

use Illuminate\Database\Eloquent\Model;

class SettlementEntryLink extends Model
{
    protected $table = 'settlement_entry_links';
    public $timestamps = false;
    protected $primaryKey = null;
    public $incrementing = false;
    protected $guarded = [];
}

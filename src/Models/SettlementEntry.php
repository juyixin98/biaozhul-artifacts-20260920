<?php

declare(strict_types=1);

namespace TravelOps\Models;

class SettlementEntry extends BaseModel
{
    protected $table = 'settlement_entries';
    public const UPDATED_AT = null;

    public const SOURCE_IMPORT = 'import';
    public const SOURCE_REVERSAL = 'reversal';
    public const SOURCE_ADJUSTMENT = 'adjustment';
}

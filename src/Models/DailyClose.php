<?php

declare(strict_types=1);

namespace Meridian\Models;

use Illuminate\Database\Eloquent\Model;

class DailyClose extends Model
{
    protected $table = 'daily_closes';
    public const UPDATED_AT = null;
    protected $fillable = ['close_date', 'closed_by'];
}

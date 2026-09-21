<?php

declare(strict_types=1);

namespace Meridian\Models;

use Illuminate\Database\Eloquent\Model;

class Contract extends Model
{
    protected $fillable = ['code', 'template_id', 'title', 'status', 'created_by'];

    public function template()
    {
        return $this->belongsTo(ContractTemplate::class, 'template_id');
    }

    public function versions()
    {
        return $this->hasMany(ContractVersion::class, 'contract_id')->orderBy('version_no');
    }

    public function events()
    {
        return $this->hasMany(ContractEvent::class, 'contract_id');
    }
}

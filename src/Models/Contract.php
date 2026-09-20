<?php

declare(strict_types=1);

namespace TravelOps\Models;

class Contract extends BaseModel
{
    protected $table = 'contracts';

    public const STATUS_DRAFT = 'draft';
    public const STATUS_PENDING = 'pending';
    public const STATUS_ACTIVE = 'active';
    public const STATUS_WITHDRAWN = 'withdrawn';
    public const STATUS_EXPIRED = 'expired';

    public function versions()
    {
        return $this->hasMany(ContractVersion::class);
    }
}

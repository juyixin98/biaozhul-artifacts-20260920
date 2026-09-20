<?php

declare(strict_types=1);

namespace TravelOps\Models;

class ContractVersion extends BaseModel
{
    protected $table = 'contract_versions';

    public const STATUS_PENDING = 'pending';
    public const STATUS_SIGNED = 'signed';
    public const STATUS_WITHDRAWN = 'withdrawn';
    public const STATUS_EXPIRED = 'expired';

    public function signatures()
    {
        return $this->hasMany(Signature::class)->orderBy('position');
    }

    public function contract()
    {
        return $this->belongsTo(Contract::class);
    }

    /** @return list<string> */
    public function signerList(): array
    {
        return json_decode((string) $this->signer_order, true, 512, JSON_THROW_ON_ERROR);
    }

    /** @return array<string,mixed> */
    public function summary(): array
    {
        return json_decode((string) $this->summary_json, true, 512, JSON_THROW_ON_ERROR);
    }

    /** @return array<string,mixed> */
    public function variables(): array
    {
        return json_decode((string) $this->variables_json, true, 512, JSON_THROW_ON_ERROR);
    }
}

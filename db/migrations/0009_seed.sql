-- Seed the two global ledger accounts. Merchant payable accounts are created
-- per merchant at merchant creation time (code PAYABLE:<merchant_id>).
INSERT INTO ledger_accounts (code, name, kind) VALUES
    ('CASH',        'Acquirer cash (asset)',          'asset'),
    ('FEE_REVENUE', 'ClearSettle fee revenue',        'revenue')
ON CONFLICT (code) DO NOTHING;

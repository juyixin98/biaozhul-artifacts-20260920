-- Seed the master packaging-proof checklist. Idempotent: codes are stable and
-- existing rows are left untouched so round snapshots remain reproducible.

INSERT INTO checklist_template_items (code, description, `order`, created_at)
VALUES
  ('COLOR',        'Color proof matches approved brand color targets (Delta E within tolerance)', 10, NOW(3)),
  ('BLEED',        'Bleed, trim and safety margins conform to dieline specification',           20, NOW(3)),
  ('TYPOGRAPHY',   'Typography, copy text and embedded fonts match the signed-off artwork',      30, NOW(3)),
  ('RESOLUTION',   'Image resolution and raster elements meet print DPI requirements',           40, NOW(3)),
  ('BARCODE',      'Barcode/QR placement, size and quiet zone are scan-ready',                   50, NOW(3)),
  ('MATERIAL',     'Substrate, finish and structural material match the purchase spec',          60, NOW(3)),
  ('REGULATORY',   'Mandatory markings, symbols and legal text are present and legible',         70, NOW(3)),
  ('FINISHING',    'Folding, gluing, embossing and other finishing lines are correctly placed',  80, NOW(3))
ON DUPLICATE KEY UPDATE description = VALUES(description);

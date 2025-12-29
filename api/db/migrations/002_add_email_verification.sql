-- Migration: Add email verification and free tier tracking
-- Run with: psql $POSTGRES_URL -f 002_add_email_verification.sql

-- Add email verification fields to accounts
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS free_tier_started_at TIMESTAMPTZ DEFAULT NOW();

-- Update existing accounts: mark as verified if they have email (grandfathering)
UPDATE accounts SET email_verified = TRUE, email_verified_at = NOW() WHERE email IS NOT NULL AND email_verified = FALSE;

-- Update products pricing to match Credits spec
-- chart2csv: 50 credits = $0.05
UPDATE products SET base_price_per_unit = 0.05000000 WHERE id = 'chart2csv';

-- masker: 1 credit = $0.001
UPDATE products SET base_price_per_unit = 0.00100000 WHERE id = 'masker';

-- Add PATAS product: 5 credits per 100 messages = $0.005/100 = $0.00005/message
INSERT INTO products (id, name, base_price_per_unit, unit_name, is_active)
VALUES ('patas', 'PATAS Anti-Spam', 0.00005000, 'message', TRUE)
ON CONFLICT (id) DO UPDATE SET base_price_per_unit = 0.00005000, name = 'PATAS Anti-Spam';

-- Add ReliAPI product: 0.1 credits = $0.0001 per request
INSERT INTO products (id, name, base_price_per_unit, unit_name, is_active)
VALUES ('reliapi', 'ReliAPI', 0.00010000, 'request', TRUE)
ON CONFLICT (id) DO UPDATE SET base_price_per_unit = 0.00010000, name = 'ReliAPI';

-- Verify
SELECT id, name, base_price_per_unit, unit_name FROM products ORDER BY id;

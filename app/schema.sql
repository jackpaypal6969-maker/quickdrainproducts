-- Quick Drain Products — SQLite schema. Idempotent: every statement is IF NOT EXISTS.
-- Money is integer cents everywhere. Timestamps are ISO-8601 UTC text.
-- The orders table is named `orders` (plural) so no query ever quotes a reserved word.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ---------------------------------------------------------------- catalog
CREATE TABLE IF NOT EXISTS collections (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  sort INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  tagline TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',          -- markdown-lite, rendered escaped
  formulation_type TEXT NOT NULL DEFAULT 'enzymatic', -- enzymatic|bacterial|caustic|acid|surfactant
  hazmat INTEGER NOT NULL DEFAULT 0,             -- 1 for caustic/acid (DOT corrosive)
  active_ingredients TEXT NOT NULL DEFAULT '',   -- verbatim from label/SDS
  net_volume_oz REAL,
  net_volume_ml REAL,
  dose_text TEXT NOT NULL DEFAULT '',            -- verbatim label dosing
  dose_interval_days INTEGER NOT NULL DEFAULT 30,
  drains_per_unit INTEGER NOT NULL DEFAULT 1,    -- one bottle treats N drains for one interval
  directions TEXT NOT NULL DEFAULT '',           -- from label; empty renders "see label"
  safe_for TEXT NOT NULL DEFAULT '',             -- comma list, label/SDS only
  not_safe_for TEXT NOT NULL DEFAULT '',
  label_claims TEXT NOT NULL DEFAULT '[]',       -- JSON array of strings copied from the label
  sds_path TEXT NOT NULL DEFAULT '',             -- relative to MEDIA_DIR; empty = not uploaded
  label_path TEXT NOT NULL DEFAULT '',
  prop65_warning TEXT NOT NULL DEFAULT '',       -- empty = no warning required
  weight_oz REAL NOT NULL DEFAULT 5,
  collection_id INTEGER REFERENCES collections(id) ON DELETE SET NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  is_featured INTEGER NOT NULL DEFAULT 0,
  sort INTEGER NOT NULL DEFAULT 0,
  seo_title TEXT NOT NULL DEFAULT '',
  seo_description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS product_images (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  base TEXT NOT NULL,           -- basename without extension under static/img/products or media/uploads
  source TEXT NOT NULL DEFAULT 'static', -- static|upload
  alt TEXT NOT NULL DEFAULT '',
  width INTEGER NOT NULL DEFAULT 1200,
  height INTEGER NOT NULL DEFAULT 1500,
  kind TEXT NOT NULL DEFAULT 'gallery', -- hero|gallery|label
  sort INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS variants (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  sku TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,                  -- Single / 3-pack / 6-pack
  units_per_pack INTEGER NOT NULL DEFAULT 1,
  price_cents INTEGER NOT NULL,
  compare_at_cents INTEGER,
  stripe_price_id TEXT NOT NULL DEFAULT '',
  stripe_subscription_price_id TEXT NOT NULL DEFAULT '',
  subscription_discount_percent INTEGER,        -- NULL = use the site-wide setting
  stock INTEGER NOT NULL DEFAULT 0,
  low_stock_threshold INTEGER NOT NULL DEFAULT 5,
  weight_oz REAL,
  is_active INTEGER NOT NULL DEFAULT 1,
  sort INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_variants_product ON variants(product_id);

CREATE TABLE IF NOT EXISTS inventory_movements (
  id INTEGER PRIMARY KEY,
  variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE RESTRICT,
  delta INTEGER NOT NULL,
  reason TEXT NOT NULL,                -- order|restock|adjust|refund|rma
  order_id INTEGER,
  admin_id INTEGER,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS product_specs (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  value TEXT NOT NULL,
  sort INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS product_faqs (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  sort INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------- customers
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL,
  email_norm TEXT NOT NULL UNIQUE,     -- lower-cased, trimmed
  password_hash TEXT NOT NULL DEFAULT '', -- empty = guest record created by an order
  first_name TEXT NOT NULL DEFAULT '',
  last_name TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',
  stripe_customer_id TEXT NOT NULL DEFAULT '',
  marketing_opt_in INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  last_login_at TEXT,
  deleted_at TEXT                       -- soft delete only; customers with orders are never hard-deleted
);

CREATE TABLE IF NOT EXISTS addresses (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  label TEXT NOT NULL DEFAULT 'Home',
  name TEXT NOT NULL DEFAULT '',
  line1 TEXT NOT NULL,
  line2 TEXT NOT NULL DEFAULT '',
  city TEXT NOT NULL,
  state TEXT NOT NULL,
  postal_code TEXT NOT NULL,
  country TEXT NOT NULL DEFAULT 'US',
  phone TEXT NOT NULL DEFAULT '',
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS password_resets (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ---------------------------------------------------------------- carts
CREATE TABLE IF NOT EXISTS carts (
  id INTEGER PRIMARY KEY,
  token TEXT NOT NULL UNIQUE,          -- random id stored in the signed session cookie
  customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
  email TEXT NOT NULL DEFAULT '',      -- captured at checkout start for abandoned-cart email
  discount_code_id INTEGER REFERENCES discount_codes(id) ON DELETE SET NULL,
  stripe_checkout_session_id TEXT NOT NULL DEFAULT '',
  checkout_lines TEXT NOT NULL DEFAULT '',   -- JSON snapshot of the lines sent to Stripe (metadata is capped at 500 chars)
  checkout_started_at TEXT,
  abandoned_email_sent_at TEXT,
  converted_order_id INTEGER,
  utm_source TEXT NOT NULL DEFAULT '',
  utm_medium TEXT NOT NULL DEFAULT '',
  utm_campaign TEXT NOT NULL DEFAULT '',
  referral_code TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_carts_customer ON carts(customer_id);
CREATE INDEX IF NOT EXISTS idx_carts_session ON carts(stripe_checkout_session_id);
CREATE INDEX IF NOT EXISTS idx_carts_abandoned ON carts(checkout_started_at, abandoned_email_sent_at, converted_order_id);

CREATE TABLE IF NOT EXISTS cart_items (
  id INTEGER PRIMARY KEY,
  cart_id INTEGER NOT NULL REFERENCES carts(id) ON DELETE CASCADE,
  variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
  qty INTEGER NOT NULL CHECK (qty > 0),
  subscribe INTEGER NOT NULL DEFAULT 0,
  UNIQUE (cart_id, variant_id, subscribe)
);

-- ---------------------------------------------------------------- discounts
CREATE TABLE IF NOT EXISTS discount_codes (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,           -- stored upper-case
  kind TEXT NOT NULL DEFAULT 'percent', -- percent|fixed|free_shipping
  value INTEGER NOT NULL DEFAULT 0,    -- percent (0-100) or cents
  min_subtotal_cents INTEGER NOT NULL DEFAULT 0,
  max_uses INTEGER,                    -- NULL = unlimited; 1 = single use
  usage_count INTEGER NOT NULL DEFAULT 0,
  restricted_to_email TEXT,            -- normalized email; NULL = anyone
  starts_at TEXT,
  expires_at TEXT,
  is_active INTEGER NOT NULL DEFAULT 1,
  channel TEXT NOT NULL DEFAULT '',    -- newsletter|abandoned|paid-meta|paid-google|print|...
  note TEXT NOT NULL DEFAULT '',
  stripe_coupon_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS discount_redemptions (
  id INTEGER PRIMARY KEY,
  discount_code_id INTEGER NOT NULL REFERENCES discount_codes(id) ON DELETE CASCADE,
  order_id INTEGER NOT NULL,
  email TEXT NOT NULL,
  redeemed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE (discount_code_id, order_id)
);

-- ---------------------------------------------------------------- orders
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY,
  order_number TEXT NOT NULL UNIQUE,   -- QD-YYMMDD-XXXX
  customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
  email TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'paid', -- paid|processing|shipped|delivered|refunded|partially_refunded|canceled
  stripe_checkout_session_id TEXT NOT NULL UNIQUE,
  stripe_payment_intent_id TEXT NOT NULL DEFAULT '',
  stripe_customer_id TEXT NOT NULL DEFAULT '',
  stripe_subscription_id TEXT NOT NULL DEFAULT '',
  currency TEXT NOT NULL DEFAULT 'usd',
  subtotal_cents INTEGER NOT NULL DEFAULT 0,
  discount_cents INTEGER NOT NULL DEFAULT 0,
  credit_cents INTEGER NOT NULL DEFAULT 0,    -- store credit applied; its own column, never a dropped line
  shipping_cents INTEGER NOT NULL DEFAULT 0,
  tax_cents INTEGER NOT NULL DEFAULT 0,
  total_cents INTEGER NOT NULL DEFAULT 0,
  refunded_cents INTEGER NOT NULL DEFAULT 0,
  discount_code_id INTEGER REFERENCES discount_codes(id) ON DELETE SET NULL,
  discount_code TEXT NOT NULL DEFAULT '',
  shipping_name TEXT NOT NULL DEFAULT '',
  shipping_line1 TEXT NOT NULL DEFAULT '',
  shipping_line2 TEXT NOT NULL DEFAULT '',
  shipping_city TEXT NOT NULL DEFAULT '',
  shipping_state TEXT NOT NULL DEFAULT '',
  shipping_postal_code TEXT NOT NULL DEFAULT '',
  shipping_country TEXT NOT NULL DEFAULT 'US',
  shipping_phone TEXT NOT NULL DEFAULT '',
  carrier TEXT NOT NULL DEFAULT '',
  tracking_number TEXT NOT NULL DEFAULT '',
  shipped_at TEXT,
  delivered_at TEXT,
  utm_source TEXT NOT NULL DEFAULT '',
  utm_medium TEXT NOT NULL DEFAULT '',
  utm_campaign TEXT NOT NULL DEFAULT '',
  referral_code TEXT,                   -- reserved for a later partner program; nothing reads it yet
  cart_id INTEGER,
  ip TEXT NOT NULL DEFAULT '',
  admin_note TEXT NOT NULL DEFAULT '',
  reorder_reminder_sent_at TEXT,
  review_invite_sent_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_email ON orders(email);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_payment_intent ON orders(stripe_payment_intent_id);
CREATE INDEX IF NOT EXISTS idx_orders_subscription ON orders(stripe_subscription_id);

CREATE TABLE IF NOT EXISTS order_items (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  variant_id INTEGER REFERENCES variants(id) ON DELETE SET NULL,
  product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
  sku TEXT NOT NULL DEFAULT '',
  product_name TEXT NOT NULL,
  variant_name TEXT NOT NULL DEFAULT '',
  qty INTEGER NOT NULL,
  unit_price_cents INTEGER NOT NULL,   -- may be 0; zero-cost lines are still inserted
  line_total_cents INTEGER NOT NULL,
  discount_cents INTEGER NOT NULL DEFAULT 0,
  credit_cents INTEGER NOT NULL DEFAULT 0,
  units_per_pack INTEGER NOT NULL DEFAULT 1,
  dose_interval_days INTEGER NOT NULL DEFAULT 30,
  drains_per_unit INTEGER NOT NULL DEFAULT 1,
  is_subscription INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);

CREATE TABLE IF NOT EXISTS processed_events (
  event_id TEXT PRIMARY KEY,
  source TEXT NOT NULL DEFAULT 'stripe',
  type TEXT NOT NULL DEFAULT '',
  processed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS subscriptions (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
  email TEXT NOT NULL,
  variant_id INTEGER REFERENCES variants(id) ON DELETE SET NULL,
  stripe_subscription_id TEXT NOT NULL UNIQUE,
  stripe_customer_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',   -- active|past_due|canceling|canceled|paused|unpaid|incomplete
  interval_days INTEGER NOT NULL DEFAULT 30,
  interval_months INTEGER NOT NULL DEFAULT 1,
  lines TEXT NOT NULL DEFAULT '[]',        -- JSON [{v,q,p,name,variant}] charged every cycle
  shipping_cents INTEGER NOT NULL DEFAULT 0,
  cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
  next_renewal_at TEXT,
  last_order_id INTEGER,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  canceled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_customer ON subscriptions(customer_id);

CREATE TABLE IF NOT EXISTS rma_requests (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  email TEXT NOT NULL,
  reason TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'requested', -- requested|approved|received|refunded|rejected
  admin_note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  resolved_at TEXT
);

-- ---------------------------------------------------------------- reviews
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
  customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
  order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
  author_name TEXT NOT NULL,
  email TEXT NOT NULL DEFAULT '',
  rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
  title TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,                  -- rendered escaped, never |safe
  is_verified INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending', -- pending|approved|rejected
  moderated_by INTEGER,
  moderated_at TEXT,
  ip TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_reviews_product_status ON reviews(product_id, status);

-- ---------------------------------------------------------------- email + lifecycle
CREATE TABLE IF NOT EXISTS newsletter_subscribers (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL DEFAULT 'modal',
  welcome_code_id INTEGER REFERENCES discount_codes(id) ON DELETE SET NULL,
  ip TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  unsubscribed_at TEXT
);

CREATE TABLE IF NOT EXISTS stock_notifications (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL,
  variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
  ip TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  notified_at TEXT,
  UNIQUE (email, variant_id)
);

CREATE TABLE IF NOT EXISTS email_suppressions (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  reason TEXT NOT NULL,                -- unsubscribe|bounce|complaint|manual
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS email_log (
  id INTEGER PRIMARY KEY,
  to_email TEXT NOT NULL,
  template TEXT NOT NULL,
  subject TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'transactional', -- transactional|marketing
  provider_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued', -- queued|sent|delivered|bounced|complained|failed|suppressed|dry_run
  error TEXT NOT NULL DEFAULT '',
  related_type TEXT NOT NULL DEFAULT '',
  related_id INTEGER,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_email_log_provider ON email_log(provider_id);
CREATE INDEX IF NOT EXISTS idx_email_log_to ON email_log(to_email);
CREATE INDEX IF NOT EXISTS idx_email_log_related ON email_log(related_type, related_id);
CREATE INDEX IF NOT EXISTS idx_discount_locked ON discount_codes(restricted_to_email, channel);
CREATE INDEX IF NOT EXISTS idx_inventory_variant ON inventory_movements(variant_id);

CREATE TABLE IF NOT EXISTS contact_messages (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  phone TEXT NOT NULL DEFAULT '',
  order_number TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',  -- new|replied|closed
  ip TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ---------------------------------------------------------------- content
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  excerpt TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',       -- markdown-lite
  cover_base TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT 'Quick Drain',
  status TEXT NOT NULL DEFAULT 'draft', -- draft|published
  published_at TEXT,
  seo_title TEXT NOT NULL DEFAULT '',
  seo_description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,           -- terms|privacy|refunds|shipping|accessibility
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',       -- markdown-lite; empty = template default
  status TEXT NOT NULL DEFAULT 'published',
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ---------------------------------------------------------------- admin + security
CREATE TABLE IF NOT EXISTS admin_users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  email TEXT NOT NULL DEFAULT '',
  password_hash TEXT NOT NULL,
  totp_secret TEXT NOT NULL DEFAULT '',
  totp_enabled INTEGER NOT NULL DEFAULT 0,
  backup_codes TEXT NOT NULL DEFAULT '[]', -- JSON array of sha256 hashes
  email_otp_hash TEXT NOT NULL DEFAULT '',
  email_otp_expires_at TEXT,
  failed_attempts INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS admin_login_attempts (
  id INTEGER PRIMARY KEY,
  ip TEXT NOT NULL,
  username TEXT NOT NULL DEFAULT '',
  success INTEGER NOT NULL DEFAULT 0,
  stage TEXT NOT NULL DEFAULT 'password', -- password|totp|email_otp|backup
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_admin_attempts ON admin_login_attempts(ip, created_at);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  actor_type TEXT NOT NULL,            -- admin|customer|system|webhook
  actor_id INTEGER,
  actor_name TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,                -- e.g. order.status_change, product.update
  target_type TEXT NOT NULL DEFAULT '',
  target_id INTEGER,
  before_json TEXT NOT NULL DEFAULT '',
  after_json TEXT NOT NULL DEFAULT '',
  ip TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target_type, target_id);

-- Shared rate limiter: fixed windows keyed by scope + subject. Shared by every worker.
CREATE TABLE IF NOT EXISTS rate_limits (
  key TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (key, window_start)
);

CREATE TABLE IF NOT EXISTS analytics_events (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  distinct_id TEXT NOT NULL DEFAULT '',
  props TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ─────────────────────────────────────────────
--  supabase_setup.sql
--  Run this in Supabase SQL Editor to create the required tables.
--  Supabase dashboard → SQL Editor → New query → paste → Run
-- ─────────────────────────────────────────────


-- ── Users table ───────────────────────────────────────────────────────────
-- Managed manually by admin — no self-registration.
-- Add users by inserting rows directly in Supabase table editor.

create table if not exists users (
    id         uuid primary key default gen_random_uuid(),
    email      text unique not null,
    credits    integer not null default 0,
    is_active  boolean not null default true,
    created_at timestamptz default now()
);

-- Index for fast email lookups
create index if not exists users_email_idx on users (email);


-- ── OTP tokens table ──────────────────────────────────────────────────────
-- Temporary records — auto-cleaned by the cleanup function below.

create table if not exists otp_tokens (
    id         uuid primary key default gen_random_uuid(),
    email      text not null,
    otp        text not null,
    expires_at timestamptz not null,
    used       boolean not null default false,
    created_at timestamptz default now()
);

-- Index for fast OTP lookups
create index if not exists otp_email_idx on otp_tokens (email, used);


-- ── Auto-cleanup expired OTPs (optional but recommended) ─────────────────
-- Deletes OTP records older than 1 hour to keep the table clean.
-- Requires pg_cron extension — enable in Supabase dashboard:
--   Extensions → pg_cron → Enable
-- Then run this to schedule cleanup every hour:

-- select cron.schedule(
--     'cleanup-expired-otps',
--     '0 * * * *',
--     $$ delete from otp_tokens where expires_at < now() - interval '1 hour' $$
-- );


-- ── Add your first user ───────────────────────────────────────────────────
-- Replace with your actual email and desired starting credits.
-- Run this after creating the tables.

-- insert into users (email, credits, is_active)
-- values ('admin@yourdomain.com', 1000, true);


-- ── Example: add a customer ───────────────────────────────────────────────
-- insert into users (email, credits, is_active)
-- values ('customer@example.com', 100, true);


-- ── Example: top up credits ───────────────────────────────────────────────
-- update users set credits = credits + 100
-- where email = 'customer@example.com';


-- ── Example: deactivate a user ────────────────────────────────────────────
-- update users set is_active = false
-- where email = 'customer@example.com';

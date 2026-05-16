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




-- ── Persisted browser sessions ───────────────────────────────────────────
-- Keeps users logged in across page refreshes. Tokens are stored hashed.

create table if not exists auth_sessions (
    id          uuid primary key default gen_random_uuid(),
    email       text not null references users(email) on delete cascade,
    token_hash  text unique not null,
    expires_at  timestamptz not null,
    revoked     boolean not null default false,
    created_at  timestamptz default now()
);

create index if not exists auth_sessions_token_idx
    on auth_sessions (token_hash, revoked, expires_at);


-- ── Credit transactions ──────────────────────────────────────────────────
-- One row per processing job. Credits are reserved before API work starts,
-- then finalized on successful extraction or refunded on extraction failure.

create table if not exists credit_transactions (
    id               uuid primary key default gen_random_uuid(),
    job_id           text unique not null,
    email            text not null references users(email) on delete cascade,
    pages            integer not null,
    credits_before   integer not null,
    credits_after    integer not null,
    credits_deducted integer not null,
    status           text not null default 'reserved',
    mode             text,
    error            text,
    finalized_at     timestamptz,
    refunded_at      timestamptz,
    created_at       timestamptz default now(),
    constraint credit_transactions_status_chk
        check (status in ('reserved', 'completed', 'refunded'))
);

-- Existing deployments created before reservation support need these columns.
alter table credit_transactions add column if not exists status text not null default 'reserved';
alter table credit_transactions add column if not exists mode text;
alter table credit_transactions add column if not exists error text;
alter table credit_transactions add column if not exists finalized_at timestamptz;
alter table credit_transactions add column if not exists refunded_at timestamptz;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'credit_transactions_status_chk'
    ) then
        alter table credit_transactions add constraint credit_transactions_status_chk
            check (status in ('reserved', 'completed', 'refunded'));
    end if;
end $$;

create index if not exists credit_transactions_email_idx
    on credit_transactions (email, created_at desc);

create index if not exists credit_transactions_status_idx
    on credit_transactions (status, created_at desc);


-- ── Credit reservation RPCs ───────────────────────────────────────────────
-- These functions perform credit changes inside one database transaction,
-- preventing concurrent browser tabs from overspending the same credits.

create or replace function reserve_credits(
    p_email text,
    p_job_id text,
    p_pages integer,
    p_mode text default null
)
returns jsonb
language plpgsql
security definer
as $$
declare
    v_email text := lower(trim(p_email));
    v_user users%rowtype;
    v_tx credit_transactions%rowtype;
    v_credits_after integer;
begin
    if p_job_id is null or trim(p_job_id) = '' then
        return jsonb_build_object('success', false, 'error', 'Missing job id');
    end if;

    if p_pages is null or p_pages <= 0 then
        return jsonb_build_object('success', false, 'error', 'Page count must be greater than zero');
    end if;

    select * into v_tx
    from credit_transactions
    where job_id = p_job_id
    for update;

    if found then
        if v_tx.status in ('reserved', 'completed') then
            return jsonb_build_object(
                'success', true,
                'already_reserved', true,
                'credits_before', v_tx.credits_before,
                'credits_after', v_tx.credits_after,
                'credits_reserved', v_tx.credits_deducted,
                'status', v_tx.status
            );
        end if;

        return jsonb_build_object(
            'success', false,
            'error', 'This job was already refunded and cannot be reserved again'
        );
    end if;

    select * into v_user
    from users
    where email = v_email
    for update;

    if not found then
        return jsonb_build_object('success', false, 'error', 'User not found');
    end if;

    if not coalesce(v_user.is_active, false) then
        return jsonb_build_object('success', false, 'error', 'User account is inactive');
    end if;

    if v_user.credits < p_pages then
        return jsonb_build_object(
            'success', false,
            'error', format('Insufficient credits: %s available, %s required', v_user.credits, p_pages),
            'credits_before', v_user.credits,
            'credits_after', v_user.credits,
            'credits_reserved', 0
        );
    end if;

    v_credits_after := v_user.credits - p_pages;

    update users
    set credits = v_credits_after
    where id = v_user.id;

    insert into credit_transactions (
        job_id, email, pages, credits_before, credits_after,
        credits_deducted, status, mode
    ) values (
        p_job_id, v_email, p_pages, v_user.credits, v_credits_after,
        p_pages, 'reserved', p_mode
    );

    return jsonb_build_object(
        'success', true,
        'already_reserved', false,
        'credits_before', v_user.credits,
        'credits_after', v_credits_after,
        'credits_reserved', p_pages,
        'status', 'reserved'
    );
end;
$$;


create or replace function finalize_credit_reservation(p_job_id text)
returns jsonb
language plpgsql
security definer
as $$
declare
    v_tx credit_transactions%rowtype;
begin
    select * into v_tx
    from credit_transactions
    where job_id = p_job_id
    for update;

    if not found then
        return jsonb_build_object('success', false, 'error', 'Credit reservation not found');
    end if;

    if v_tx.status = 'completed' then
        return jsonb_build_object('success', true, 'status', 'completed');
    end if;

    if v_tx.status = 'refunded' then
        return jsonb_build_object('success', false, 'status', 'refunded', 'error', 'Reservation was already refunded');
    end if;

    update credit_transactions
    set status = 'completed', finalized_at = now(), error = null
    where job_id = p_job_id;

    return jsonb_build_object('success', true, 'status', 'completed');
end;
$$;


create or replace function refund_credit_reservation(
    p_job_id text,
    p_reason text default null
)
returns jsonb
language plpgsql
security definer
as $$
declare
    v_tx credit_transactions%rowtype;
    v_credits_before integer;
    v_credits_after integer;
begin
    select * into v_tx
    from credit_transactions
    where job_id = p_job_id
    for update;

    if not found then
        return jsonb_build_object('success', false, 'error', 'Credit reservation not found');
    end if;

    if v_tx.status = 'refunded' then
        return jsonb_build_object(
            'success', true,
            'already_refunded', true,
            'credits_before', v_tx.credits_after,
            'credits_after', v_tx.credits_after,
            'credits_refunded', 0,
            'status', 'refunded'
        );
    end if;

    if v_tx.status = 'completed' then
        return jsonb_build_object('success', false, 'status', 'completed', 'error', 'Completed jobs cannot be refunded automatically');
    end if;

    select credits into v_credits_before
    from users
    where email = v_tx.email
    for update;

    update users
    set credits = credits + v_tx.credits_deducted
    where email = v_tx.email
    returning credits into v_credits_after;

    update credit_transactions
    set status = 'refunded',
        credits_after = v_credits_after,
        refunded_at = now(),
        error = p_reason
    where job_id = p_job_id;

    return jsonb_build_object(
        'success', true,
        'already_refunded', false,
        'credits_before', v_credits_before,
        'credits_after', v_credits_after,
        'credits_refunded', v_tx.credits_deducted,
        'status', 'refunded'
    );
end;
$$;


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

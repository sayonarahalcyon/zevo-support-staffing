-- ZEVO Support Staffing: vacation leave log.
-- Run this once in the Supabase dashboard: SQL Editor -> New query -> paste -> Run.
-- Safe to run again; it only creates what is missing.

create extension if not exists btree_gist;

create table if not exists public.leaves (
    id            uuid primary key default gen_random_uuid(),
    agent_code    text not null,
    start_date    date not null,
    end_date      date not null,
    note          text,
    status        text not null default 'active' check (status in ('active', 'cancelled')),
    created_by    text not null,
    created_at    timestamptz not null default now(),
    updated_by    text,
    updated_at    timestamptz not null default now(),
    cancelled_by  text,
    cancelled_at  timestamptz,
    constraint leaves_dates_ordered check (end_date >= start_date),
    -- Two active leaves for the same agent cannot overlap. The app checks this
    -- first and shows a friendly message; this is the safety net if two people
    -- save at the same moment.
    constraint leaves_no_overlap exclude using gist (
        agent_code with =,
        daterange(start_date, end_date, '[]') with &&
    ) where (status = 'active')
);

create index if not exists leaves_dates_idx on public.leaves (start_date, end_date);

create table if not exists public.leave_audit (
    id         bigserial primary key,
    leave_id   uuid not null,
    action     text not null check (action in ('added', 'edited', 'cancelled')),
    actor      text,
    at         timestamptz not null default now(),
    before     jsonb,
    after      jsonb
);

create index if not exists leave_audit_leave_idx on public.leave_audit (leave_id, at desc);

-- Keep updated_at / cancelled_* correct no matter which client writes.
create or replace function public.leaves_before_update() returns trigger as $$
begin
    new.updated_at := now();
    if new.status = 'cancelled' and old.status = 'active' then
        new.cancelled_at := now();
        new.cancelled_by := coalesce(new.updated_by, old.updated_by, old.created_by);
    end if;
    return new;
end;
$$ language plpgsql;

drop trigger if exists leaves_before_update on public.leaves;
create trigger leaves_before_update before update on public.leaves
    for each row execute function public.leaves_before_update();

-- Every add, edit and cancel is recorded in the same transaction as the change,
-- so the history can never miss one.
create or replace function public.leaves_audit() returns trigger as $$
begin
    if tg_op = 'INSERT' then
        insert into public.leave_audit (leave_id, action, actor, after)
        values (new.id, 'added', new.created_by, to_jsonb(new));
    elsif tg_op = 'UPDATE' then
        insert into public.leave_audit (leave_id, action, actor, before, after)
        values (
            new.id,
            case when new.status = 'cancelled' and old.status = 'active' then 'cancelled' else 'edited' end,
            coalesce(new.updated_by, old.created_by),
            to_jsonb(old),
            to_jsonb(new)
        );
    end if;
    return null;
end;
$$ language plpgsql;

drop trigger if exists leaves_audit on public.leaves;
create trigger leaves_audit after insert or update on public.leaves
    for each row execute function public.leaves_audit();

-- Lock the tables down. The app uses the project's secret/service key on the
-- server side, which bypasses row level security. With RLS on and no policies,
-- the public (anon) key cannot read or write anything here.
alter table public.leaves enable row level security;
alter table public.leave_audit enable row level security;

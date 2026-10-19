-- The support agent's whole schema, for the T2 Ticketing System.
--
-- A fresh start: nothing from the OTRS database is carried over. Ticket and
-- article IDs are the ticket system's GUIDs (t2_*). From here on every schema
-- change is a new numbered file after this one; never edit this file.

CREATE FUNCTION touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TYPE article_party AS ENUM ('request', 'other');
CREATE TYPE doc_status    AS ENUM ('pending', 'indexed', 'failed');
CREATE TYPE draft_action  AS ENUM ('answer', 'route');
CREATE TYPE draft_status  AS ENUM ('pending', 'approved', 'rejected', 'superseded', 'not_ours');
CREATE TYPE reply_kind    AS ENUM ('answer', 'ask_more');
CREATE TYPE review_outcome AS ENUM ('approve', 'edit', 'reject', 'not_ours');
CREATE TYPE write_status  AS ENUM ('pending', 'sent', 'failed');


-- ── People ───────────────────────────────────────────────────────────────

CREATE TABLE reviewers (
    id            bigserial PRIMARY KEY,
    email         text NOT NULL UNIQUE,
    display_name  text,
    password_hash text NOT NULL,
    is_active     boolean NOT NULL DEFAULT true,
    is_admin      boolean NOT NULL DEFAULT false,
    permissions   text[] NOT NULL DEFAULT ARRAY['review', 'tables'],
    last_seen_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    token       text PRIMARY KEY,
    reviewer_id bigint NOT NULL REFERENCES reviewers(id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    last_seen   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_sessions_expiry   ON sessions (expires_at);
CREATE INDEX idx_sessions_reviewer ON sessions (reviewer_id);

CREATE TABLE signup_codes (
    email         text PRIMARY KEY,
    code_hash     text NOT NULL,
    password_hash text NOT NULL,
    display_name  text,
    attempts      integer NOT NULL DEFAULT 0,
    expires_at    timestamptz NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reset_codes (
    email      text PRIMARY KEY,
    code_hash  text NOT NULL,
    attempts   integer NOT NULL DEFAULT 0,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);


-- ── Settings and the agent's values ──────────────────────────────────────

-- Values a super admin set on the Settings page; they win over .env.
-- Passwords and keys are stored encrypted (secret = true).
CREATE TABLE app_settings (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    secret     boolean NOT NULL DEFAULT false,
    updated_by text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- What we add to the ticket system's values. The values themselves -- the
-- products, queues, types, subtypes, statuses and priorities -- are read live
-- from its TicketLookupsGet; a row here is our side of one of them, matched
-- by name.
-- product:     the product it belongs to (NULL for statuses and priorities,
--              which every product shares).
-- parent:      the type a subtype sits under.
-- description: when to use it, in our words. What the agent decides by; a
--              value with none is still offered, by its name alone.
-- conclusion:  for a subtype, the text written to the ticket's Conclusions
--              (for RiCH, the reason from their catalog).
-- hidden:      never offered to the agent (the type "Default", the intake
--              product itself ...).
-- first_seen_at / seen_at: a value is "New" on the Values page from when it
--              first appeared until someone writes its description or marks
--              it seen.
-- removed_at:  no longer in the ticket system. Not offered; the row and its
--              description are kept in case it comes back.
CREATE TABLE agent_options (
    id            bigserial PRIMARY KEY,
    kind          text NOT NULL CHECK (kind IN ('product', 'queue', 'next_state', 'type', 'subtype', 'priority')),
    product       text,
    parent        text,
    value         text NOT NULL CHECK (length(btrim(value)) > 0),
    description   text CHECK (description IS NULL OR length(btrim(description)) > 0),
    conclusion    text,
    hidden        boolean NOT NULL DEFAULT false,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    seen_at       timestamptz,
    removed_at    timestamptz,
    updated_by    bigint REFERENCES reviewers(id),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CHECK (kind <> 'subtype' OR parent IS NOT NULL)
);
CREATE INDEX idx_agent_options_kind ON agent_options (kind, product);
CREATE INDEX idx_agent_options_new ON agent_options (first_seen_at)
    WHERE seen_at IS NULL AND description IS NULL AND removed_at IS NULL;
CREATE UNIQUE INDEX uq_agent_options_value
    ON agent_options (kind, coalesce(product, ''), coalesce(parent, ''), lower(value));
CREATE TRIGGER agent_options_touch BEFORE UPDATE ON agent_options
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE agent_option_kinds (
    kind       text PRIMARY KEY CHECK (kind IN ('product', 'queue', 'next_state', 'type', 'subtype', 'priority')),
    guidance   text NOT NULL CHECK (length(btrim(guidance)) > 0),
    updated_by bigint REFERENCES reviewers(id),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER agent_option_kinds_touch BEFORE UPDATE ON agent_option_kinds
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE team_scopes (
    id          bigserial PRIMARY KEY,
    content     text NOT NULL CHECK (length(btrim(content)) > 0),
    filename    text,
    uploaded_by bigint REFERENCES reviewers(id),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE documents (
    id            bigserial PRIMARY KEY,
    filename      text NOT NULL,
    content_hash  text NOT NULL UNIQUE,
    size_bytes    bigint,
    uploaded_by   bigint NOT NULL REFERENCES reviewers(id),
    status        doc_status NOT NULL DEFAULT 'pending',
    ragent_doc_id text,
    chunk_count   integer,
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    indexed_at    timestamptz
);
CREATE INDEX idx_documents_status ON documents (status, created_at);


-- ── Open tickets the agent is working on ─────────────────────────────────

CREATE TABLE tickets (
    id                      bigserial PRIMARY KEY,
    t2_ticket_id            text NOT NULL UNIQUE,
    ticket_number           text NOT NULL UNIQUE,
    title                   text,
    product                 text,
    product_code            text,
    queue                   text,
    state                   text,
    state_type              text,
    type                    text,
    priority                text,
    sla                     text,
    subtype                 text,
    conclusions             text,
    requires_classification boolean NOT NULL DEFAULT false,
    requester_email         text NOT NULL,
    customer_id             text,
    customer_contact_status text,
    customer_org            text,
    customer_tier           text,
    is_vip                  boolean NOT NULL DEFAULT false,
    t2_created_at           timestamptz,
    first_seen_at           timestamptz NOT NULL DEFAULT now(),
    last_synced_at          timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    search_text             text,
    search_ver              text
);
CREATE TRIGGER tickets_touch BEFORE UPDATE ON tickets
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE articles (
    id             bigserial PRIMARY KEY,
    ticket_id      bigint NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    t2_article_id  text NOT NULL UNIQUE,
    article_no     integer,
    party          article_party NOT NULL,
    via            text,
    sender_name    text,
    sender_address text,
    recipients     text,
    cc             text,
    subject        text,
    body_raw       text NOT NULL,
    body_clean     text,
    body_quoted    text,
    body_signature text,
    attachments    jsonb NOT NULL DEFAULT '[]',
    t2_created_at  timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_articles_ticket ON articles (ticket_id, article_no);


-- ── Drafts, reviews and what was written back ────────────────────────────

CREATE TABLE drafts (
    id                  bigserial PRIMARY KEY,
    ticket_id           bigint NOT NULL REFERENCES tickets(id),
    status              draft_status NOT NULL DEFAULT 'pending',
    action              draft_action NOT NULL,
    reply_kind          reply_kind,
    reply_body          text,
    product             text NOT NULL,
    queue               text NOT NULL,
    queue_reason        text,
    next_state          text NOT NULL,
    type                text,
    subtype             text,
    conclusions         text,
    priority            text,
    issue_type          text,
    summary             text NOT NULL,
    collected           jsonb NOT NULL DEFAULT '{}',
    missing             jsonb NOT NULL DEFAULT '[]',
    note                text,
    confidence          text,
    reasoning           text,
    evidence            jsonb,
    trace               jsonb NOT NULL DEFAULT '[]',
    similar_status      text CHECK (similar_status IN ('found', 'none', 'failed', 'no_index')),
    model               text,
    prompt_ver          text,
    latency_ms          integer,
    built_to_article_id bigint REFERENCES articles(id),
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_drafts_pending ON drafts (created_at) WHERE status = 'pending';
CREATE UNIQUE INDEX uq_drafts_one_pending ON drafts (ticket_id) WHERE status = 'pending';

CREATE TABLE reviews (
    id             bigserial PRIMARY KEY,
    draft_id       bigint NOT NULL UNIQUE REFERENCES drafts(id),
    reviewer_id    bigint NOT NULL REFERENCES reviewers(id),
    outcome        review_outcome NOT NULL,
    final          jsonb,
    changed_fields text[] NOT NULL DEFAULT '{}',
    changes        jsonb NOT NULL DEFAULT '{}',
    justification  text,
    comment        text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_reviews_reviewer ON reviews (reviewer_id, created_at);

-- One TicketUpdate per approved draft, retried until it is written.
CREATE TABLE ticket_writes (
    id            bigserial PRIMARY KEY,
    draft_id      bigint NOT NULL UNIQUE REFERENCES drafts(id),
    payload       jsonb NOT NULL,
    status        write_status NOT NULL DEFAULT 'pending',
    attempts      smallint NOT NULL DEFAULT 0,
    last_error    text,
    t2_article_id text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    delivered_at  timestamptz
);
CREATE INDEX idx_ticket_writes_pending ON ticket_writes (created_at) WHERE status = 'pending';


-- ── Checks made on each draft ────────────────────────────────────────────

CREATE TABLE draft_urgent (
    draft_id     bigint PRIMARY KEY REFERENCES drafts(id) ON DELETE CASCADE,
    by_keyword   boolean NOT NULL DEFAULT false,
    keywords     text[] NOT NULL DEFAULT '{}',
    by_agent     boolean NOT NULL DEFAULT false,
    agent_reason text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE draft_junk (
    draft_id    bigint PRIMARY KEY REFERENCES drafts(id) ON DELETE CASCADE,
    score       real NOT NULL DEFAULT 0,
    matches     jsonb NOT NULL DEFAULT '[]',
    sender_junk integer NOT NULL DEFAULT 0,
    sender_real integer NOT NULL DEFAULT 0,
    flagged     boolean NOT NULL DEFAULT false,
    reason      text,
    threshold   real,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_draft_junk_flagged ON draft_junk (flagged) WHERE flagged;


-- ── Urgent alerts ────────────────────────────────────────────────────────

CREATE TABLE urgent_keywords (
    id         bigserial PRIMARY KEY,
    keyword    text NOT NULL CHECK (length(btrim(keyword)) > 0),
    active     boolean NOT NULL DEFAULT true,
    added_by   bigint REFERENCES reviewers(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_urgent_keyword ON urgent_keywords (lower(btrim(keyword)));
CREATE TRIGGER urgent_keywords_touch BEFORE UPDATE ON urgent_keywords
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE urgent_recipients (
    id         bigserial PRIMARY KEY,
    email      text NOT NULL CHECK (position('@' IN email) > 1),
    name       text,
    active     boolean NOT NULL DEFAULT true,
    added_by   bigint REFERENCES reviewers(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_urgent_recipient ON urgent_recipients (lower(btrim(email)));
CREATE TRIGGER urgent_recipients_touch BEFORE UPDATE ON urgent_recipients
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE urgent_emails (
    id             bigserial PRIMARY KEY,
    draft_id       bigint REFERENCES drafts(id) ON DELETE SET NULL,
    ticket_id      bigint REFERENCES tickets(id) ON DELETE SET NULL,
    to_address     text NOT NULL,
    subject        text NOT NULL,
    body           text NOT NULL,
    status         text NOT NULL CHECK (status IN ('sent', 'dry_run', 'skipped', 'failed')),
    error          text,
    rfc_message_id text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_urgent_emails_draft ON urgent_emails (draft_id);


-- ── History: closed tickets the agent learns from ────────────────────────

CREATE TABLE closed_tickets (
    id               bigserial PRIMARY KEY,
    t2_ticket_id     text NOT NULL UNIQUE,
    ticket_number    text NOT NULL UNIQUE,
    title            text,
    product          text,
    product_code     text,
    queue            text,
    state            text,
    type             text,
    priority         text,
    sla              text,
    subtype          text,
    conclusions      text,
    requester_email  text,
    customer_id      text,
    customer_org     text,
    t2_created_at    timestamptz,
    t2_closed_at     timestamptz,
    solution_minutes integer,
    article_count    integer NOT NULL,
    noise_count      integer NOT NULL,
    dynamic_fields   jsonb NOT NULL DEFAULT '{}',
    history          jsonb NOT NULL DEFAULT '[]',
    fetched_at       timestamptz NOT NULL DEFAULT now(),
    search_text      text,
    search_ver       text,
    junk_check       text CHECK (junk_check IN ('kept', 'repeat', 'real_copy')),
    junk_check_ref   text,
    junk_check_score real,
    junk_fingerprint text,
    junk_checked_at  timestamptz
);
CREATE INDEX idx_closed_queue   ON closed_tickets (queue, t2_closed_at);
CREATE INDEX idx_closed_subtype ON closed_tickets (subtype);
CREATE INDEX idx_closed_junk_fingerprint ON closed_tickets (junk_fingerprint) WHERE junk_check = 'kept';

CREATE TABLE closed_articles (
    id               bigserial PRIMARY KEY,
    closed_ticket_id bigint NOT NULL REFERENCES closed_tickets(id) ON DELETE CASCADE,
    t2_article_id    text NOT NULL UNIQUE,
    article_no       integer,
    sender_type      text,
    channel_id       text,
    visible          boolean,
    sender_name      text,
    sender_address   text,
    recipients       text,
    cc               text,
    subject          text,
    body_raw         text NOT NULL,
    body_clean       text,
    body_quoted      text,
    body_signature   text,
    attachments      jsonb NOT NULL DEFAULT '[]',
    t2_created_at    timestamptz
);
CREATE INDEX idx_closed_articles_ticket ON closed_articles (closed_ticket_id, article_no);

-- Messages and queue moves in order, cleaned.
CREATE TABLE ticket_timelines (
    closed_ticket_id bigint PRIMARY KEY REFERENCES closed_tickets(id) ON DELETE CASCADE,
    entries          jsonb NOT NULL,
    queue_path       text[] NOT NULL DEFAULT '{}',
    article_count    integer NOT NULL,
    kept_count       integer NOT NULL,
    history_count    integer NOT NULL,
    noise_count      integer NOT NULL,
    empty_count      integer NOT NULL DEFAULT 0,
    clean_ver        text NOT NULL,
    fetched_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ticket_journeys (
    id               bigserial PRIMARY KEY,
    closed_ticket_id bigint NOT NULL UNIQUE REFERENCES closed_tickets(id) ON DELETE CASCADE,
    path             text[] NOT NULL DEFAULT '{}',
    steps            jsonb NOT NULL DEFAULT '[]',
    moves            jsonb NOT NULL DEFAULT '[]',
    summary          text,
    model            text,
    fallback_reason  text,
    prompt_ver       text,
    input_chars      integer,
    latency_ms       integer,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ticket_scopes (
    closed_ticket_id bigint NOT NULL REFERENCES closed_tickets(id) ON DELETE CASCADE,
    team             text NOT NULL,
    involved         boolean NOT NULL DEFAULT false,
    did              text,
    passed_on        text,
    model            text,
    fallback_reason  text,
    prompt_ver       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (closed_ticket_id, team)
);
CREATE INDEX idx_ticket_scopes_team ON ticket_scopes (team) WHERE involved;

CREATE TABLE draft_similar (
    draft_id         bigint NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    closed_ticket_id bigint NOT NULL REFERENCES closed_tickets(id) ON DELETE CASCADE,
    rank             smallint NOT NULL,
    score            real,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (draft_id, closed_ticket_id)
);
CREATE INDEX idx_draft_similar_draft ON draft_similar (draft_id, rank);

CREATE TABLE boilerplate_lines (
    scope    text NOT NULL CHECK (scope IN ('domain', 'sender', 'internal')),
    key      text NOT NULL,
    line_key text NOT NULL,
    line     text NOT NULL,
    seen_in  integer NOT NULL,
    out_of   integer NOT NULL,
    built_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, key, line_key)
);
CREATE INDEX idx_boilerplate_internal ON boilerplate_lines (scope) WHERE scope = 'internal';

CREATE TABLE weekly_runs (
    id           bigserial PRIMARY KEY,
    week_key     text NOT NULL UNIQUE,
    status       text NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    window_start timestamptz NOT NULL,
    window_end   timestamptz NOT NULL,
    summary      jsonb NOT NULL DEFAULT '{}',
    report       text,
    emailed_to   text[] NOT NULL DEFAULT '{}',
    error        text,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);


-- ── Audit log: what a person did ─────────────────────────────────────────
-- Only decisions and changes made by people: approving, editing, rejecting
-- or setting aside a draft, uploading a file, and adding, removing or
-- changing a value. Nothing the agent does on its own is written here.
--   who:   the person's email
--   what:  what was acted on ('draft', 'setting', 'queue', 'urgent keyword', ...)
--   name:  which one (the setting key, the value, the file name, ...)
--   old_value / new_value: before and after, for a change (secrets masked)
CREATE TABLE audit_log (
    id         bigserial PRIMARY KEY,
    who        text NOT NULL,
    action     text NOT NULL CHECK (action IN ('approve', 'edit', 'reject', 'not_ours',
                                               'upload', 'add', 'remove', 'change')),
    what       text NOT NULL,
    name       text,
    old_value  text,
    new_value  text,
    ticket_id  bigint REFERENCES tickets(id) ON DELETE SET NULL,
    draft_id   bigint REFERENCES drafts(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_created ON audit_log (created_at DESC);
CREATE INDEX idx_audit_ticket  ON audit_log (ticket_id, created_at);

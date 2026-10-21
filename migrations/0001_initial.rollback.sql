DROP TABLE IF EXISTS
    audit_log, weekly_runs, boilerplate_lines, draft_similar, ticket_scopes,
    ticket_journeys, ticket_timelines, closed_articles, closed_tickets,
    urgent_emails, urgent_recipients, urgent_keywords, draft_spam, draft_urgent,
    ticket_writes, reviews, drafts, articles, tickets, documents, team_scopes,
    agent_option_kinds, agent_options, app_settings, console_sessions, console_users;

DROP TYPE IF EXISTS write_status, review_outcome, reply_kind, draft_status,
    draft_action, doc_status, article_party;

DROP FUNCTION IF EXISTS touch_updated_at();

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL UNIQUE,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    full_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'member',
    status TEXT NOT NULL DEFAULT 'pending',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, email)
);

CREATE TABLE IF NOT EXISTS campaigns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    channel TEXT NOT NULL DEFAULT 'email',
    target_query TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS leads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    campaign_id UUID REFERENCES campaigns(id) ON DELETE SET NULL,
    job_id UUID,
    company_url TEXT NOT NULL DEFAULT '',
    verified_email TEXT NOT NULL DEFAULT '',
    service_reason TEXT NOT NULL DEFAULT '',
    outreach_status TEXT NOT NULL DEFAULT 'pending',
    followup_count INTEGER NOT NULL DEFAULT 0,
    reply_status TEXT NOT NULL DEFAULT 'no_reply',
    last_reply_at TIMESTAMPTZ,
    company_name TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    raw_html TEXT NOT NULL DEFAULT '',
    cleaned_text TEXT NOT NULL DEFAULT '',
    ai_response JSONB NOT NULL DEFAULT '{}'::jsonb,
    company_summary TEXT NOT NULL DEFAULT '',
    needs_it_services BOOLEAN NOT NULL DEFAULT FALSE,
    lead_score INTEGER NOT NULL DEFAULT 0 CHECK (lead_score BETWEEN 0 AND 10),
    score INTEGER NOT NULL DEFAULT 0,
    buying_intent_score INTEGER NOT NULL DEFAULT 0 CHECK (buying_intent_score BETWEEN 0 AND 100),
    service_demand_score INTEGER NOT NULL DEFAULT 0 CHECK (service_demand_score BETWEEN 0 AND 100),
    urgency_score INTEGER NOT NULL DEFAULT 0 CHECK (urgency_score BETWEEN 0 AND 100),
    intent_summary TEXT NOT NULL DEFAULT '',
    signals JSONB NOT NULL DEFAULT '[]'::jsonb,
    lifecycle_state TEXT NOT NULL DEFAULT 'discovered',
    status TEXT NOT NULL DEFAULT 'pending',
    rejection_reason TEXT NOT NULL DEFAULT '',
    source_query TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, email, website),
    CONSTRAINT valid_lifecycle CHECK (
        lifecycle_state IN (
            'discovered', 'scraped', 'cleaned', 'scored',
            'qualified', 'rejected', 'outreach_pending',
            'outreach_sent', 'replied', 'follow_up_scheduled',
            'follow_up_sent', 'converted', 'unsubscribed', 'dead'
        )
    )
);

CREATE TABLE IF NOT EXISTS emails (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    campaign_id UUID REFERENCES campaigns(id) ON DELETE SET NULL,
    lead_id UUID REFERENCES leads(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT 'gmail',
    provider_message_id TEXT NOT NULL DEFAULT '',
    provider_thread_id TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT 'outbound',
    status TEXT NOT NULL DEFAULT 'draft',
    sent_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS replies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    campaign_id UUID REFERENCES campaigns(id) ON DELETE SET NULL,
    lead_id UUID REFERENCES leads(id) ON DELETE CASCADE,
    email_id UUID REFERENCES emails(id) ON DELETE CASCADE,
    provider_message_id TEXT NOT NULL DEFAULT '',
    provider_thread_id TEXT NOT NULL DEFAULT '',
    from_email TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    classification TEXT NOT NULL DEFAULT '',
    sentiment TEXT NOT NULL DEFAULT '',
    lead_temperature TEXT NOT NULL DEFAULT '',
    received_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS followups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    campaign_id UUID REFERENCES campaigns(id) ON DELETE SET NULL,
    lead_id UUID REFERENCES leads(id) ON DELETE CASCADE,
    email_id UUID REFERENCES emails(id) ON DELETE SET NULL,
    sequence_step INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'scheduled',
    scheduled_for TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    agent_name TEXT NOT NULL DEFAULT '',
    job_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    input_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    job_type TEXT NOT NULL DEFAULT 'discovery',
    queue TEXT NOT NULL DEFAULT 'default',
    status TEXT NOT NULL DEFAULT 'queued',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT NOT NULL DEFAULT '',
    error_log JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    max_retries INTEGER NOT NULL DEFAULT 3,
    locked_by TEXT NOT NULL DEFAULT '',
    scheduled_for TIMESTAMPTZ,
    locked_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT valid_status CHECK (
        status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'retrying', 'queued')
    ),
    CONSTRAINT valid_job_type CHECK (
        job_type IN (
            'discovery', 'scraping', 'cleaning', 'scoring',
            'outreach_generation', 'gmail_send', 'reply_monitor',
            'follow_up', 'full_pipeline'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_users_tenant_id ON users (tenant_id);
CREATE INDEX IF NOT EXISTS idx_users_tenant_role ON users (tenant_id, role);

CREATE INDEX IF NOT EXISTS idx_campaigns_tenant_id ON campaigns (tenant_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_tenant_status ON campaigns (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_campaigns_owner_user_id ON campaigns (owner_user_id);

CREATE INDEX IF NOT EXISTS idx_leads_tenant_id ON leads (tenant_id);
CREATE INDEX IF NOT EXISTS idx_leads_tenant_campaign_id ON leads (tenant_id, campaign_id);
CREATE INDEX IF NOT EXISTS idx_leads_tenant_status ON leads (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_leads_tenant_score ON leads (tenant_id, score DESC);
CREATE INDEX IF NOT EXISTS idx_leads_tenant_email ON leads (tenant_id, email);

CREATE INDEX IF NOT EXISTS idx_emails_tenant_id ON emails (tenant_id);
CREATE INDEX IF NOT EXISTS idx_emails_tenant_lead_id ON emails (tenant_id, lead_id);
CREATE INDEX IF NOT EXISTS idx_emails_tenant_campaign_id ON emails (tenant_id, campaign_id);
CREATE INDEX IF NOT EXISTS idx_emails_tenant_status ON emails (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_emails_provider_thread ON emails (tenant_id, provider_thread_id);
CREATE INDEX IF NOT EXISTS idx_emails_sent_at ON emails (tenant_id, sent_at DESC);

CREATE INDEX IF NOT EXISTS idx_replies_tenant_id ON replies (tenant_id);
CREATE INDEX IF NOT EXISTS idx_replies_tenant_lead_id ON replies (tenant_id, lead_id);
CREATE INDEX IF NOT EXISTS idx_replies_tenant_email_id ON replies (tenant_id, email_id);
CREATE INDEX IF NOT EXISTS idx_replies_provider_thread ON replies (tenant_id, provider_thread_id);
CREATE INDEX IF NOT EXISTS idx_replies_received_at ON replies (tenant_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_replies_classification ON replies (tenant_id, classification);

CREATE INDEX IF NOT EXISTS idx_followups_tenant_id ON followups (tenant_id);
CREATE INDEX IF NOT EXISTS idx_followups_tenant_lead_id ON followups (tenant_id, lead_id);
CREATE INDEX IF NOT EXISTS idx_followups_tenant_status ON followups (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_followups_scheduled_for ON followups (tenant_id, scheduled_for);

CREATE INDEX IF NOT EXISTS idx_agent_runs_tenant_id ON agent_runs (tenant_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_tenant_status ON agent_runs (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_id ON jobs (tenant_id);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_status ON jobs (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_queue_status ON jobs (queue, status, created_at);

CREATE TABLE IF NOT EXISTS gmail_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    email_address TEXT NOT NULL DEFAULT '',
    credentials_json TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gmail_credentials_tenant_id ON gmail_credentials (tenant_id);

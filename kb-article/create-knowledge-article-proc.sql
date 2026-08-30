-- Create tables + procedures for persisting knowledge articles to billingservice
-- Companion to the E2E test feedback system (create-openclaw-e2e-test-feedback-proc.sql)
-- Run this script in the billingservice Postgres database on CT100:
-- psql -h 192.168.21.153 -p 5432 -U ai_easybutt0n -d billingservice -f create-knowledge-article-proc.sql

-- ────────────────────────────────────────────────────────────────────
-- Tables
-- ────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS knowledge_articles (
    article_id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general'
        CHECK (category IN (
            'general',
            'troubleshooting',
            'architecture',
            'decision',
            'procedure',
            'infrastructure',
            'debugging',
            'configuration'
        )),
    tags TEXT[] NOT NULL DEFAULT '{}',
    content TEXT NOT NULL,
    source_file TEXT,
    author TEXT,
    status TEXT NOT NULL DEFAULT 'published'
        CHECK (status IN ('draft', 'published', 'superseded', 'archived')),
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    git_sha TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_knowledge_articles_category
ON knowledge_articles (category);

CREATE INDEX IF NOT EXISTS idx_knowledge_articles_status
ON knowledge_articles (status);

CREATE INDEX IF NOT EXISTS idx_knowledge_articles_tags
ON knowledge_articles USING GIN (tags);

CREATE INDEX IF NOT EXISTS idx_knowledge_articles_created
ON knowledge_articles (created_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_article_feedback (
    feedback_id BIGSERIAL PRIMARY KEY,
    article_id BIGINT NOT NULL REFERENCES knowledge_articles (article_id) ON DELETE CASCADE,
    feedback_type TEXT NOT NULL
        CHECK (feedback_type IN ('confirmed', 'outdated', 'incorrect', 'expanded', 'question')),
    feedback_text TEXT,
    reviewer TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_knowledge_article_feedback_article
ON knowledge_article_feedback (article_id, created_at DESC);

-- ────────────────────────────────────────────────────────────────────
-- Stored procedures
-- ────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION create_knowledge_article(
    p_title TEXT,
    p_category TEXT DEFAULT 'general',
    p_tags TEXT[] DEFAULT '{}',
    p_content TEXT DEFAULT '',
    p_source_file TEXT DEFAULT NULL,
    p_author TEXT DEFAULT NULL,
    p_status TEXT DEFAULT 'published',
    p_metadata JSONB DEFAULT '{}'::JSONB,
    p_git_sha TEXT DEFAULT NULL
) RETURNS BIGINT
LANGUAGE plpgsql
AS $$
DECLARE
    v_article_id BIGINT;
BEGIN
    INSERT INTO knowledge_articles (
        title,
        category,
        tags,
        content,
        source_file,
        author,
        status,
        metadata,
        git_sha
    )
    VALUES (
        NULLIF(TRIM(p_title), ''),
        COALESCE(NULLIF(TRIM(p_category), ''), 'general'),
        COALESCE(p_tags, '{}'),
        COALESCE(p_content, ''),
        NULLIF(TRIM(p_source_file), ''),
        NULLIF(TRIM(p_author), ''),
        COALESCE(NULLIF(TRIM(p_status), ''), 'published'),
        COALESCE(p_metadata, '{}'::JSONB),
        NULLIF(TRIM(p_git_sha), '')
    )
    RETURNING article_id INTO v_article_id;

    RETURN v_article_id;
END;
$$;

CREATE OR REPLACE FUNCTION update_knowledge_article(
    p_article_id BIGINT,
    p_title TEXT DEFAULT NULL,
    p_category TEXT DEFAULT NULL,
    p_tags TEXT[] DEFAULT NULL,
    p_content TEXT DEFAULT NULL,
    p_status TEXT DEFAULT NULL,
    p_metadata JSONB DEFAULT NULL,
    p_git_sha TEXT DEFAULT NULL
) RETURNS BIGINT
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE knowledge_articles
    SET
        title = COALESCE(NULLIF(TRIM(p_title), ''), title),
        category = COALESCE(NULLIF(TRIM(p_category), ''), category),
        tags = COALESCE(p_tags, tags),
        content = COALESCE(p_content, content),
        status = COALESCE(NULLIF(TRIM(p_status), ''), status),
        metadata = COALESCE(p_metadata, metadata),
        git_sha = COALESCE(NULLIF(TRIM(p_git_sha), ''), git_sha),
        updated_at = NOW()
    WHERE article_id = p_article_id;

    RETURN p_article_id;
END;
$$;

CREATE OR REPLACE FUNCTION record_knowledge_article_feedback(
    p_article_id BIGINT,
    p_feedback_type TEXT,
    p_feedback_text TEXT DEFAULT NULL,
    p_reviewer TEXT DEFAULT NULL,
    p_metadata JSONB DEFAULT '{}'::JSONB
) RETURNS BIGINT
LANGUAGE plpgsql
AS $$
DECLARE
    v_feedback_id BIGINT;
BEGIN
    INSERT INTO knowledge_article_feedback (
        article_id,
        feedback_type,
        feedback_text,
        reviewer,
        metadata
    )
    VALUES (
        p_article_id,
        NULLIF(TRIM(p_feedback_type), ''),
        NULLIF(TRIM(p_feedback_text), ''),
        NULLIF(TRIM(p_reviewer), ''),
        COALESCE(p_metadata, '{}'::JSONB)
    )
    RETURNING feedback_id INTO v_feedback_id;

    RETURN v_feedback_id;
END;
$$;

CREATE OR REPLACE FUNCTION get_knowledge_article(p_article_id BIGINT)
RETURNS TABLE (
    article_id BIGINT,
    title TEXT,
    category TEXT,
    tags TEXT[],
    content TEXT,
    source_file TEXT,
    author TEXT,
    status TEXT,
    metadata JSONB,
    git_sha TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    feedback_count BIGINT
)
LANGUAGE sql
AS $$
    SELECT
        a.article_id,
        a.title,
        a.category,
        a.tags,
        a.content,
        a.source_file,
        a.author,
        a.status,
        a.metadata,
        a.git_sha,
        a.created_at,
        a.updated_at,
        COALESCE((SELECT COUNT(*) FROM knowledge_article_feedback f WHERE f.article_id = a.article_id), 0)
    FROM knowledge_articles a
    WHERE a.article_id = p_article_id;
$$;

CREATE OR REPLACE FUNCTION search_knowledge_articles(
    p_query TEXT DEFAULT NULL,
    p_category TEXT DEFAULT NULL,
    p_tag TEXT DEFAULT NULL,
    p_status TEXT DEFAULT 'published',
    p_limit INTEGER DEFAULT 20
)
RETURNS TABLE (
    article_id BIGINT,
    title TEXT,
    category TEXT,
    tags TEXT[],
    status TEXT,
    author TEXT,
    source_file TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    feedback_count BIGINT
)
LANGUAGE sql
AS $$
    SELECT
        a.article_id,
        a.title,
        a.category,
        a.tags,
        a.status,
        a.author,
        a.source_file,
        a.created_at,
        a.updated_at,
        COALESCE((SELECT COUNT(*) FROM knowledge_article_feedback f WHERE f.article_id = a.article_id), 0)
    FROM knowledge_articles a
    WHERE
        (p_status IS NULL OR a.status = p_status)
        AND (p_category IS NULL OR a.category = p_category)
        AND (p_tag IS NULL OR p_tag = ANY(a.tags))
        AND (p_query IS NULL OR (
            a.title ILIKE '%' || p_query || '%'
            OR a.content ILIKE '%' || p_query || '%'
        ))
    ORDER BY a.updated_at DESC
    LIMIT COALESCE(p_limit, 20);
$$;

CREATE OR REPLACE FUNCTION get_knowledge_article_feedback(p_article_id BIGINT)
RETURNS TABLE (
    feedback_id BIGINT,
    feedback_type TEXT,
    feedback_text TEXT,
    reviewer TEXT,
    metadata JSONB,
    created_at TIMESTAMPTZ
)
LANGUAGE sql
AS $$
    SELECT
        f.feedback_id,
        f.feedback_type,
        f.feedback_text,
        f.reviewer,
        f.metadata,
        f.created_at
    FROM knowledge_article_feedback f
    WHERE f.article_id = p_article_id
    ORDER BY f.created_at ASC;
$$;

CREATE OR REPLACE FUNCTION get_latest_knowledge_articles(
    p_limit INTEGER DEFAULT 10
)
RETURNS TABLE (
    article_id BIGINT,
    title TEXT,
    category TEXT,
    tags TEXT[],
    status TEXT,
    author TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    feedback_count BIGINT
)
LANGUAGE sql
AS $$
    SELECT
        a.article_id,
        a.title,
        a.category,
        a.tags,
        a.status,
        a.author,
        a.created_at,
        a.updated_at,
        COALESCE((SELECT COUNT(*) FROM knowledge_article_feedback f WHERE f.article_id = a.article_id), 0)
    FROM knowledge_articles a
    WHERE a.status = 'published'
    ORDER BY a.updated_at DESC
    LIMIT COALESCE(p_limit, 10);
$$;

-- ────────────────────────────────────────────────────────────────────
-- Comments
-- ────────────────────────────────────────────────────────────────────

COMMENT ON TABLE knowledge_articles IS
    'Persistent knowledge articles for the Cognizioware project — troubleshooting, architecture decisions, procedures, and debugging insights.';

COMMENT ON TABLE knowledge_article_feedback IS
    'Feedback loop entries for knowledge articles — confirmed, outdated, incorrect, expanded, or questions.';

COMMENT ON FUNCTION create_knowledge_article(TEXT, TEXT, TEXT[], TEXT, TEXT, TEXT, TEXT, JSONB, TEXT) IS
    'Create a new knowledge article and return the generated article_id.';

COMMENT ON FUNCTION update_knowledge_article(BIGINT, TEXT, TEXT, TEXT[], TEXT, TEXT, JSONB, TEXT) IS
    'Update an existing knowledge article (only non-NULL params are applied).';

COMMENT ON FUNCTION record_knowledge_article_feedback(BIGINT, TEXT, TEXT, TEXT, JSONB) IS
    'Record feedback (confirmed/outdated/incorrect/expanded/question) for a knowledge article.';

COMMENT ON FUNCTION get_knowledge_article(BIGINT) IS
    'Return full details for a single knowledge article including feedback count.';

COMMENT ON FUNCTION search_knowledge_articles(TEXT, TEXT, TEXT, TEXT, INTEGER) IS
    'Search knowledge articles by free-text query, category, tag, and status.';

COMMENT ON FUNCTION get_knowledge_article_feedback(BIGINT) IS
    'Return all feedback entries for a knowledge article.';

COMMENT ON FUNCTION get_latest_knowledge_articles(INTEGER) IS
    'Return the most recently updated published knowledge articles.';

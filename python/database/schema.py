"""
BARQ Database Schema - SQL table definitions for all modules.

Table design follows the PRD's data model covering:
- User settings & preferences
- Job search (listings, evaluations, applications)
- Social media (trends, scripts, videos, posts)
- Analytics (career funnel, social performance, revenue)
- Voice commands and activity logging
"""

# ─── User Settings ───────────────────────────────────────────────────────────

CREATE_USER_SETTINGS = """
CREATE TABLE IF NOT EXISTS user_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'general',
    is_encrypted INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_USER_PROFILES = """
CREATE TABLE IF NOT EXISTS user_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    linkedin_url TEXT NOT NULL DEFAULT '',
    portfolio_url TEXT NOT NULL DEFAULT '',
    github_url TEXT NOT NULL DEFAULT '',
    headline TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    skills TEXT NOT NULL DEFAULT '[]',          -- JSON array of strings
    experience TEXT NOT NULL DEFAULT '[]',       -- JSON array of experience objects
    education TEXT NOT NULL DEFAULT '[]',        -- JSON array of education objects
    experience_level TEXT NOT NULL DEFAULT 'mid'
        CHECK (experience_level IN ('entry', 'mid', 'senior', 'lead', 'executive')),
    target_salary_min INTEGER DEFAULT 0,
    target_salary_max INTEGER DEFAULT 0,
    preferred_locations TEXT NOT NULL DEFAULT '[]', -- JSON array
    remote_preference TEXT NOT NULL DEFAULT 'any'
        CHECK (remote_preference IN ('any', 'remote', 'hybrid', 'onsite')),
    preferred_industries TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# ─── Job Search ──────────────────────────────────────────────────────────────

CREATE_JOB_LISTINGS = """
CREATE TABLE IF NOT EXISTS job_listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT NOT NULL DEFAULT '',         -- ID from the source board
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    salary_min INTEGER DEFAULT 0,
    salary_max INTEGER DEFAULT 0,
    salary_currency TEXT NOT NULL DEFAULT 'USD',
    salary_period TEXT NOT NULL DEFAULT 'yearly'
        CHECK (salary_period IN ('yearly', 'monthly', 'hourly', 'contract')),
    employment_type TEXT NOT NULL DEFAULT 'full_time'
        CHECK (employment_type IN ('full_time', 'part_time', 'contract', 'internship', 'temporary')),
    remote_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (remote_status IN ('remote', 'hybrid', 'onsite', 'unknown')),
    source_board TEXT NOT NULL,                   -- 'linkedin', 'indeed', etc.
    source_url TEXT NOT NULL DEFAULT '',
    posted_date TEXT,
    expires_date TEXT,
    company_logo_url TEXT NOT NULL DEFAULT '',
    company_rating REAL DEFAULT 0.0,
    skills_required TEXT NOT NULL DEFAULT '[]',   -- JSON array
    fingerprint TEXT NOT NULL DEFAULT '',         -- dedup hash: normalized title|company|location
    is_active INTEGER NOT NULL DEFAULT 1,
    scanned_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_JOB_EVALUATIONS = """
CREATE TABLE IF NOT EXISTS job_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_listing_id INTEGER NOT NULL,
    overall_score REAL NOT NULL DEFAULT 0.0
        CHECK (overall_score >= 0 AND overall_score <= 5),
    role_fit_score REAL NOT NULL DEFAULT 0.0
        CHECK (role_fit_score >= 0 AND role_fit_score <= 5),
    culture_score REAL NOT NULL DEFAULT 0.0
        CHECK (culture_score >= 0 AND culture_score <= 5),
    compensation_score REAL NOT NULL DEFAULT 0.0
        CHECK (compensation_score >= 0 AND compensation_score <= 5),
    growth_score REAL NOT NULL DEFAULT 0.0
        CHECK (growth_score >= 0 AND growth_score <= 5),
    red_flag_score REAL NOT NULL DEFAULT 0.0
        CHECK (red_flag_score >= 0 AND red_flag_score <= 5),
    match_percentage REAL NOT NULL DEFAULT 0.0
        CHECK (match_percentage >= 0 AND match_percentage <= 100),
    reasoning TEXT NOT NULL DEFAULT '',
    pros TEXT NOT NULL DEFAULT '[]',              -- JSON array
    cons TEXT NOT NULL DEFAULT '[]',              -- JSON array
    evaluated_by TEXT NOT NULL DEFAULT 'llm'
        CHECK (evaluated_by IN ('llm', 'keyword', 'manual', 'scanner')),
    evaluated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (job_listing_id) REFERENCES job_listings(id) ON DELETE CASCADE
);
"""

CREATE_APPLICATIONS = """
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_listing_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN (
            'draft', 'queued', 'generating', 'ready_for_review',
            'approved', 'submitted', 'rejected', 'withdrawn', 'failed'
        )),
    application_type TEXT NOT NULL DEFAULT 'auto'
        CHECK (application_type IN ('auto', 'manual', 'quick_apply')),
    submission_method TEXT NOT NULL DEFAULT 'unknown'
        CHECK (submission_method IN ('unknown', 'direct', 'linkedin_easy', 'indeed_apply', 'company_portal', 'email')),
    resume_path TEXT NOT NULL DEFAULT '',
    cover_letter_path TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    submitted_at TEXT,
    response_received_at TEXT,
    response_type TEXT
        CHECK (response_type IN ('interview', 'rejection', 'assessment', 'offer', 'none')),
    interview_date TEXT,
    notified_at TEXT,                             -- when the last Telegram/notification was sent
    retry_count INTEGER NOT NULL DEFAULT 0,       -- auto-retry up to 3 times on failure
    offer_details TEXT NOT NULL DEFAULT '{}',     -- JSON
    rejection_reason TEXT NOT NULL DEFAULT '',
    score REAL DEFAULT 0.0
        CHECK (score >= 0 AND score <= 5),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (job_listing_id) REFERENCES job_listings(id) ON DELETE CASCADE
);
"""

CREATE_APPLICATION_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS application_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL,
    document_type TEXT NOT NULL
        CHECK (document_type IN ('resume', 'cover_letter', 'portfolio', 'other')),
    content TEXT NOT NULL,                        -- Full markdown/text content
    file_path TEXT NOT NULL DEFAULT '',           -- Path to generated PDF/DOCX if exported
    format TEXT NOT NULL DEFAULT 'markdown'
        CHECK (format IN ('markdown', 'pdf', 'docx', 'txt', 'html')),
    version INTEGER NOT NULL DEFAULT 1,
    is_active INTEGER NOT NULL DEFAULT 1,
    generated_by TEXT NOT NULL DEFAULT 'llm'
        CHECK (generated_by IN ('llm', 'template', 'manual')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
);
"""

# ─── Social Media Content ────────────────────────────────────────────────────

CREATE_TRENDS = """
CREATE TABLE IF NOT EXISTS trends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source TEXT NOT NULL
        CHECK (source IN ('reddit', 'twitter', 'news', 'google_trends', 'manual', 'github', 'product_hunt')),
    subreddit TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL DEFAULT 0.0,
    engagement INTEGER NOT NULL DEFAULT 0,
    niche TEXT NOT NULL DEFAULT 'technology',
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_CONTENT_SCRIPTS = """
CREATE TABLE IF NOT EXISTS content_scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trend_id INTEGER,
    title TEXT NOT NULL,
    topic TEXT NOT NULL,
    format TEXT NOT NULL
        CHECK (format IN (
            'tiktok_short', 'youtube_shorts', 'youtube_essay',
            'instagram_reel', 'twitter_thread', 'linkedin_post', 'blog_post'
        )),
    tone TEXT NOT NULL DEFAULT 'professional'
        CHECK (tone IN ('professional', 'casual', 'humorous', 'educational', 'inspirational')),
    estimated_duration_seconds INTEGER DEFAULT 60,
    script_content TEXT NOT NULL,                 -- Full script with visual cues
    sections TEXT NOT NULL DEFAULT '[]',          -- JSON array of section names
    visual_cues TEXT NOT NULL DEFAULT '[]',       -- JSON array of visual cues
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'finalized', 'rendering', 'rendered', 'published')),
    score INTEGER DEFAULT 0
        CHECK (score >= 0 AND score <= 100),
    revised INTEGER NOT NULL DEFAULT 0,        -- 1 when the W7 content critic revised the draft
    gate_iterations INTEGER NOT NULL DEFAULT 0, -- how many critic revise cycles ran
    generated_by TEXT NOT NULL DEFAULT 'llm'
        CHECK (generated_by IN ('llm', 'template', 'manual')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (trend_id) REFERENCES trends(id) ON DELETE SET NULL
);
"""

CREATE_VIDEOS = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    script_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL,
    file_size_bytes INTEGER DEFAULT 0,
    duration_seconds REAL DEFAULT 0.0,
    resolution TEXT NOT NULL DEFAULT '1080x1920',
    format TEXT NOT NULL DEFAULT 'mp4'
        CHECK (format IN ('mp4', 'mov', 'webm')),
    status TEXT NOT NULL DEFAULT 'rendering'
        CHECK (status IN ('rendering', 'completed', 'failed', 'posted', 'archived')),
    error_message TEXT NOT NULL DEFAULT '',
    voiceover_path TEXT NOT NULL DEFAULT '',
    stock_footage_used TEXT NOT NULL DEFAULT '[]',  -- JSON array
    render_time_seconds REAL DEFAULT 0.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (script_id) REFERENCES content_scripts(id) ON DELETE CASCADE
);
"""

CREATE_POSTS = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL,
    platform TEXT NOT NULL
        CHECK (platform IN ('youtube', 'tiktok', 'instagram', 'twitter', 'linkedin')),
    platform_post_id TEXT NOT NULL DEFAULT '',    -- ID from the platform
    platform_url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'posting', 'posted', 'failed', 'scheduled')),
    scheduled_at TEXT,
    posted_at TEXT,
    error_message TEXT NOT NULL DEFAULT '',
    engagement_metrics TEXT NOT NULL DEFAULT '{}',  -- JSON: views, likes, comments, shares
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
);
"""

# ─── Analytics ───────────────────────────────────────────────────────────────

CREATE_CAREER_ANALYTICS_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS career_analytics_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,
    jobs_scanned INTEGER NOT NULL DEFAULT 0,
    matches_found INTEGER NOT NULL DEFAULT 0,
    applications_sent INTEGER NOT NULL DEFAULT 0,
    interviews_scheduled INTEGER NOT NULL DEFAULT 0,
    offers_received INTEGER NOT NULL DEFAULT 0,
    active_applications INTEGER NOT NULL DEFAULT 0,
    avg_response_time_days REAL DEFAULT 0.0,
    top_sources TEXT NOT NULL DEFAULT '[]',       -- JSON
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_SOCIAL_ANALYTICS_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS social_analytics_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,
    platform TEXT NOT NULL,
    followers INTEGER NOT NULL DEFAULT 0,
    total_views INTEGER NOT NULL DEFAULT 0,
    total_engagement INTEGER NOT NULL DEFAULT 0,
    engagement_rate REAL DEFAULT 0.0,
    videos_posted INTEGER NOT NULL DEFAULT 0,
    revenue REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_REVENUE_RECORDS = """
CREATE TABLE IF NOT EXISTS revenue_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL
        CHECK (source IN (
            'youtube_adsense', 'tiktok_creator_fund', 'instagram_bonuses',
            'affiliate_links', 'sponsorships', 'other'
        )),
    platform TEXT NOT NULL,
    amount REAL NOT NULL DEFAULT 0.0
        CHECK (amount >= 0),
    currency TEXT NOT NULL DEFAULT 'USD',
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'received', 'verified', 'disputed')),
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# ─── Voice & Activity ────────────────────────────────────────────────────────

CREATE_VOICE_COMMANDS = """
CREATE TABLE IF NOT EXISTS voice_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transcript TEXT NOT NULL,
    confidence REAL DEFAULT 0.0
        CHECK (confidence >= 0 AND confidence <= 1),
    action TEXT NOT NULL DEFAULT 'unknown',
    action_target TEXT NOT NULL DEFAULT '',
    was_wake_word INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_ACTIVITY_LOG = """
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL
        CHECK (type IN ('job', 'content', 'analytics', 'voice', 'system', 'notification', 'error', 'memory', 'desktop', 'web', 'documents', 'research')),
    action TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',           -- JSON for extra context
    severity TEXT NOT NULL DEFAULT 'info'
        CHECK (severity IN ('debug', 'info', 'warning', 'error', 'critical')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_NOTIFICATIONS = """
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL
        CHECK (channel IN ('telegram', 'email', 'desktop', 'all')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal'
        CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    category TEXT NOT NULL DEFAULT 'general'
        CHECK (category IN ('general', 'job_match', 'application', 'content', 'analytics', 'error', 'system')),
    related_entity_type TEXT,
    related_entity_id INTEGER,
    sent INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT,
    read INTEGER NOT NULL DEFAULT 0,
    read_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# ─── New v2.0 Tables ─────────────────────────────────────────────────────────

CREATE_SCHEDULED_TASKS = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL
        CHECK (task_type IN (
            'job_scan', 'trend_check', 'content_post', 'analytics_snapshot',
            'digest_email', 'custom'
        )),
    name TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',
    cron_expression TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run TEXT,
    next_run TEXT,
    total_runs INTEGER NOT NULL DEFAULT 0,
    last_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (last_status IN ('pending', 'running', 'completed', 'failed')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_WIDGETS = """
CREATE TABLE IF NOT EXISTS widgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    widget_type TEXT NOT NULL
        CHECK (widget_type IN ('timer', 'stock_ticker', 'calculator', 'weather',
                               'notes', 'clock', 'custom')),
    config TEXT NOT NULL DEFAULT '{}',
    position_x INTEGER NOT NULL DEFAULT 100,
    position_y INTEGER NOT NULL DEFAULT 100,
    width INTEGER NOT NULL DEFAULT 300,
    height INTEGER NOT NULL DEFAULT 200,
    visible INTEGER NOT NULL DEFAULT 1,
    z_index INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_FILE_INDEX = """
CREATE TABLE IF NOT EXISTS file_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL DEFAULT '',
    file_size_bytes INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    embedding BLOB,
    tags TEXT NOT NULL DEFAULT '[]',
    last_indexed TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_COMPANY_RESEARCH = """
CREATE TABLE IF NOT EXISTS company_research (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL UNIQUE,
    summary TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    headquarters TEXT NOT NULL DEFAULT '',
    founded_year INTEGER,
    employee_count INTEGER DEFAULT 0,
    funding_total REAL DEFAULT 0.0,
    funding_rounds TEXT NOT NULL DEFAULT '[]',
    glassdoor_rating REAL DEFAULT 0.0
        CHECK (glassdoor_rating >= 0 AND glassdoor_rating <= 5),
    linkedin_url TEXT NOT NULL DEFAULT '',
    crunchbase_url TEXT NOT NULL DEFAULT '',
    tech_stack TEXT NOT NULL DEFAULT '[]',
    recent_news TEXT NOT NULL DEFAULT '[]',
    competitors TEXT NOT NULL DEFAULT '[]',
    researched_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_NOTES = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    pinned INTEGER NOT NULL DEFAULT 0,
    color TEXT NOT NULL DEFAULT 'default',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_ACTION_LOG = """
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    description TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info'
        CHECK (severity IN ('info', 'warning', 'danger')),
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# ─── Agentic Workflows (v3.0) ───────────────────────────────────────────────

CREATE_AGENT_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS agent_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checkpoint_key TEXT UNIQUE NOT NULL,        -- e.g. 'agent:<task_id>' or 'workflow:<run_id>'
    agent_type TEXT NOT NULL DEFAULT 'agent',   -- 'agent' | 'workflow'
    data TEXT NOT NULL DEFAULT '{}',            -- JSON payload (plan, steps, results)
    status TEXT NOT NULL DEFAULT 'active',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_WORKFLOWS = """
CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    definition TEXT NOT NULL DEFAULT '{}',      -- JSON workflow definition
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# ─── Aggregate schema creation ───────────────────────────────────────────────


CREATE_ENTITY_IMAGES = """
CREATE TABLE IF NOT EXISTS entity_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brain_id TEXT NOT NULL DEFAULT '',
    entity TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_SYNC_MAPPINGS = """
CREATE TABLE IF NOT EXISTS sync_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brain_type TEXT NOT NULL DEFAULT 'general',        -- BARQ brain domain
    entity_name TEXT NOT NULL,                          -- BARQ entity name
    second_brain_item_id INTEGER,                       -- Second Brain item ID
    content_hash TEXT NOT NULL DEFAULT '',               -- hash of last-synced content for change detection
    direction TEXT NOT NULL DEFAULT 'both'
        CHECK (direction IN ('to_items', 'to_triplets', 'both')),
    last_synced_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(brain_type, entity_name, second_brain_item_id)
);
"""

CREATE_SYNC_LOG = """
CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_type TEXT NOT NULL
        CHECK (sync_type IN ('to_items', 'to_triplets', 'full')),
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed')),
    items_synced INTEGER NOT NULL DEFAULT 0,
    items_created INTEGER NOT NULL DEFAULT 0,
    items_updated INTEGER NOT NULL DEFAULT 0,
    items_skipped INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);
"""

ALL_TABLES = [
    ("sync_mappings", CREATE_SYNC_MAPPINGS),
    ("sync_log", CREATE_SYNC_LOG),
    ("user_settings", CREATE_USER_SETTINGS),
    ("user_profiles", CREATE_USER_PROFILES),
    ("notes", CREATE_NOTES),
    ("action_log", CREATE_ACTION_LOG),
    ("job_listings", CREATE_JOB_LISTINGS),
    ("job_evaluations", CREATE_JOB_EVALUATIONS),
    ("applications", CREATE_APPLICATIONS),
    ("application_documents", CREATE_APPLICATION_DOCUMENTS),
    ("trends", CREATE_TRENDS),
    ("content_scripts", CREATE_CONTENT_SCRIPTS),
    ("videos", CREATE_VIDEOS),
    ("posts", CREATE_POSTS),
    ("career_analytics_snapshots", CREATE_CAREER_ANALYTICS_SNAPSHOTS),
    ("social_analytics_snapshots", CREATE_SOCIAL_ANALYTICS_SNAPSHOTS),
    ("revenue_records", CREATE_REVENUE_RECORDS),
    ("voice_commands", CREATE_VOICE_COMMANDS),
    ("activity_log", CREATE_ACTIVITY_LOG),
    ("notifications", CREATE_NOTIFICATIONS),
    ("scheduled_tasks", CREATE_SCHEDULED_TASKS),
    ("widgets", CREATE_WIDGETS),
    ("file_index", CREATE_FILE_INDEX),
    ("company_research", CREATE_COMPANY_RESEARCH),
    ("agent_checkpoints", CREATE_AGENT_CHECKPOINTS),
    ("workflows", CREATE_WORKFLOWS),
    ("entity_images", CREATE_ENTITY_IMAGES),
]


# ─── In-place migrations ────────────────────────────────────────────────────
# CREATE TABLE IF NOT EXISTS won't add columns to databases created before a
# schema change, so older installs get an idempotent ALTER TABLE instead.

SCHEMA_MIGRATIONS = [
    # Applications: track when a job was last notified so pipeline runs skip
    # already-notified applications (stops repeated Telegram alerts).
    (
        "applications",
        "notified_at",
        "ALTER TABLE applications ADD COLUMN notified_at TEXT",
    ),
    # Job dedup: fingerprint column so boards without stable ids/urls can be deduped
    (
        "job_listings",
        "fingerprint",
        "ALTER TABLE job_listings ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''",
    ),
    # W7 quality gate: persist whether the content critic revised the draft
    (
        "content_scripts",
        "revised",
        "ALTER TABLE content_scripts ADD COLUMN revised INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "content_scripts",
        "gate_iterations",
        "ALTER TABLE content_scripts ADD COLUMN gate_iterations INTEGER NOT NULL DEFAULT 0",
    ),
    # Pipeline retry: track retry count for failed applications
    (
        "applications",
        "retry_count",
        "ALTER TABLE applications ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
    ),
]


# ─── Job-listing dedup indexes (v2) ────────────────────────────────────────
# Partial UNIQUE indexes so a job re-found on the next scan maps to the same
# row. Partial (WHERE) keeps rows with empty keys from colliding with each
# other. These can only be created once existing duplicates are collapsed —
# run scripts/dedupe_jobs.py first if creation fails.

JOB_LISTING_DEDUP_INDEXES = [
    (
        "uq_job_listings_board_external",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_job_listings_board_external "
        "ON job_listings(source_board, external_id) WHERE external_id != ''",
    ),
    (
        "uq_job_listings_source_url",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_job_listings_source_url "
        "ON job_listings(source_url) WHERE source_url != ''",
    ),
    (
        "uq_job_listings_fingerprint",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_job_listings_fingerprint "
        "ON job_listings(fingerprint) WHERE fingerprint != ''",
    ),
]


async def _ensure_column(db, table: str, column: str, ddl: str) -> None:
    """Add a column to an existing table if it isn't already present."""
    try:
        cols = await db.fetch_all(f"PRAGMA table_info({table})")
    except Exception as e:
        print(f"[Schema] Could not inspect '{table}' columns: {e}")
        return
    if any(c.get("name") == column for c in cols):
        return
    try:
        await db.execute(ddl)
        await db.commit()
        print(f"[Schema] Migration: added column '{column}' to '{table}'")
    except Exception as e:
        print(f"[Schema] Migration failed for '{table}.{column}': {e}")


async def initialize_schema(db):
    """Create all tables if they don't exist."""
    for table_name, ddl in ALL_TABLES:
        try:
            await db.execute(ddl)
            print(f"[Schema] Table '{table_name}' ready")
        except Exception as e:
            print(f"[Schema] Error creating table '{table_name}': {e}")

    # Apply in-place column migrations to existing databases
    for table, column, ddl in SCHEMA_MIGRATIONS:
        await _ensure_column(db, table, column, ddl)

    # Create job-listing dedup indexes (partial UNIQUE indexes). These can
    # fail on databases that already contain duplicates — run
    # scripts/dedupe_jobs.py first. Failures are logged, not fatal.
    # Entity image history lookup index (queries are always brain + entity scoped)
    try:
        await db.execute(
            'CREATE INDEX IF NOT EXISTS idx_entity_images_lookup '
            'ON entity_images(brain_id, entity)'
        )
    except Exception as e:
        print(f'[Schema] Warning: entity_images index not created: {e}')

    for index_name, ddl in JOB_LISTING_DEDUP_INDEXES:
        try:
            await db.execute(ddl)
            print(f"[Schema] Index '{index_name}' ready")
        except Exception as e:
            print(f"[Schema] Warning: index '{index_name}' not created: {e}")

    await db.commit()
    print(f"[Schema] All {len(ALL_TABLES)} tables initialized")


async def seed_defaults(db):
    """Insert default data if tables are empty."""
    # Default user profile if none exists
    row = await db.fetch_one("SELECT COUNT(*) as count FROM user_profiles")
    if row and row["count"] == 0:
        await db.execute("""
            INSERT INTO user_profiles (full_name, email, headline, skills)
            VALUES ('User', '', 'Professional', '["Communication"]')
        """)

    # Default settings
    default_settings = [
        ("wake_word", "computer", "voice"),
        ("whisper_model", "base", "voice"),
        ("tts_voice", "en-US-JennyNeural", "voice"),
        ("wake_word_sensitivity", "medium", "voice"),
        ("wake_sound_enabled", "true", "voice"),
        ("command_sound_enabled", "true", "voice"),
        ("job_scan_interval", "6", "jobs"),
        ("match_threshold", "0.7", "jobs"),
        ("match_threshold_high", "80", "jobs"),
        ("match_threshold_medium", "60", "jobs"),
        ("preferred_location", "remote", "jobs"),
        ("auto_apply_enabled", "false", "jobs"),
        ("trend_check_interval", "6", "social"),
        ("default_platforms", "youtube,tiktok", "social"),
        ("auto_post_enabled", "false", "social"),
        ("daily_digest_enabled", "true", "notifications"),
        ("telegram_enabled", "false", "notifications"),
        ("email_enabled", "false", "notifications"),
        ("desktop_notifications", "true", "notifications"),
        ("job_match_alerts", "true", "notifications"),
        ("content_alerts", "true", "notifications"),
        ("local_processing_only", "true", "privacy"),
        ("analytics_opt_in", "false", "privacy"),
        ("crash_reporting", "false", "privacy"),
        ("accent_color", "cyan", "appearance"),
        ("theme", "dark", "appearance"),
        ("wake_greeting_enabled", "true", "voice"),
        ("animations_enabled", "true", "appearance"),
        ("voice_agent_backend", "gemini", "voice"),
    ]

    for key, value, category in default_settings:
        result = await db.fetch_one(
            "SELECT COUNT(*) as count FROM user_settings WHERE key = ?", (key,)
        )
        if result and result["count"] == 0:
            await db.execute(
                "INSERT INTO user_settings (key, value, category) VALUES (?, ?, ?)",
                (key, value, category),
            )

    # Seed default scheduled tasks
    row = await db.fetch_one("SELECT COUNT(*) as count FROM scheduled_tasks")
    if row and row["count"] == 0:
        default_tasks = [
            ("job_scan", "Auto Job Scan", '{"keywords": ["software engineer", "developer", "full stack"], "location": "remote"}', "0 */6 * * *"),
            ("trend_check", "Trend Check", '{"niche": "technology"}', "0 */6 * * *"),
            ("analytics_snapshot", "Daily Analytics", "{}", "0 0 * * *"),
            ("digest_email", "Daily Digest", "{}", "0 8 * * *"),
        ]
        for task_type, name, config, cron in default_tasks:
            await db.execute(
                "INSERT INTO scheduled_tasks (task_type, name, config, cron_expression) VALUES (?, ?, ?, ?)",
                (task_type, name, config, cron),
            )

    await db.commit()
    print("[Schema] Default data seeded")

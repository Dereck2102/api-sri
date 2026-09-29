CREATE TABLE organizations (
	organization_id TEXT PRIMARY KEY,
	name TEXT NOT NULL,
	status TEXT NOT NULL DEFAULT 'invited' CHECK (status IN ('invited', 'active', 'suspended', 'revoked')),
	created_at TEXT NOT NULL,
	updated_at TEXT NOT NULL
);

CREATE TABLE integrations (
	integration_id TEXT PRIMARY KEY,
	organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
	name TEXT NOT NULL,
	status TEXT NOT NULL DEFAULT 'invited' CHECK (status IN ('invited', 'active', 'suspended', 'revoked')),
	permissions TEXT NOT NULL DEFAULT '["ruc:read"]' CHECK (json_valid(permissions)),
	requests_per_minute INTEGER NOT NULL CHECK (requests_per_minute > 0),
	daily_limit INTEGER NOT NULL CHECK (daily_limit > 0),
	created_at TEXT NOT NULL,
	updated_at TEXT NOT NULL
);

CREATE TABLE integration_credentials (
	credential_id TEXT PRIMARY KEY,
	integration_id TEXT NOT NULL REFERENCES integrations(integration_id),
	secret_hash TEXT NOT NULL UNIQUE CHECK (length(secret_hash) = 64),
	status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked', 'expired')),
	created_at TEXT NOT NULL,
	expires_at TEXT,
	revoked_at TEXT,
	rotated_from TEXT REFERENCES integration_credentials(credential_id)
);
CREATE INDEX integration_credentials_integration_idx
	ON integration_credentials(integration_id, status);

CREATE TABLE internal_users (
	email TEXT PRIMARY KEY COLLATE NOCASE,
	status TEXT NOT NULL DEFAULT 'invited' CHECK (status IN ('invited', 'active', 'revoked')),
	role TEXT NOT NULL CHECK (role IN ('creator', 'support', 'read_only')),
	invited_by TEXT,
	created_at TEXT NOT NULL,
	updated_at TEXT NOT NULL
);

CREATE TABLE province_catalog (
	province_code TEXT PRIMARY KEY,
	province_name TEXT NOT NULL UNIQUE,
	enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
	active_version_id INTEGER REFERENCES dataset_versions(version_id),
	updated_at TEXT NOT NULL
);

CREATE TABLE source_catalog (
	source_id TEXT PRIMARY KEY,
	authority TEXT NOT NULL,
	province_code TEXT REFERENCES province_catalog(province_code),
	source_url TEXT NOT NULL,
	enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
	status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready', 'active', 'stale', 'failed', 'disabled')),
	created_at TEXT NOT NULL,
	updated_at TEXT NOT NULL
);

CREATE TABLE dataset_versions (
	version_id INTEGER PRIMARY KEY,
	version_key TEXT NOT NULL UNIQUE,
	source_id TEXT NOT NULL REFERENCES source_catalog(source_id),
	province_code TEXT NOT NULL REFERENCES province_catalog(province_code),
	status TEXT NOT NULL CHECK (status IN ('staging', 'validated', 'active', 'superseded', 'failed')),
	source_period TEXT,
	source_updated_at TEXT,
	observed_at TEXT NOT NULL,
	archive_sha256 TEXT,
	archive_bytes INTEGER,
	uncompressed_bytes INTEGER,
	row_count INTEGER NOT NULL DEFAULT 0,
	unique_ruc_count INTEGER NOT NULL DEFAULT 0,
	normalized_bytes INTEGER,
	detail TEXT
);
CREATE INDEX dataset_versions_province_source_idx
	ON dataset_versions(province_code, source_id, observed_at);

CREATE TABLE lookup_values (
	value_id INTEGER PRIMARY KEY,
	kind TEXT NOT NULL,
	value TEXT NOT NULL,
	UNIQUE (kind, value)
);

CREATE TABLE economic_activities (
	activity_id INTEGER PRIMARY KEY,
	ciiu_code TEXT NOT NULL,
	description TEXT NOT NULL DEFAULT '',
	UNIQUE (ciiu_code, description)
);

CREATE TABLE ruc_snapshot (
	version_id INTEGER NOT NULL REFERENCES dataset_versions(version_id),
	ruc TEXT NOT NULL CHECK (length(ruc) = 13),
	legal_name TEXT,
	tax_status_id INTEGER REFERENCES lookup_values(value_id),
	taxpayer_class_id INTEGER REFERENCES lookup_values(value_id),
	taxpayer_type_id INTEGER REFERENCES lookup_values(value_id),
	accounting_required_id INTEGER REFERENCES lookup_values(value_id),
	main_economic_activity_id INTEGER REFERENCES economic_activities(activity_id),
	commercial_name TEXT,
	start_date TEXT,
	cessation_date TEXT,
	resumption_date TEXT,
	taxpayer_updated_at TEXT,
	establishments_count INTEGER NOT NULL DEFAULT 1 CHECK (establishments_count > 0),
	preferred_establishment INTEGER NOT NULL DEFAULT 0 CHECK (preferred_establishment IN (0, 1)),
	PRIMARY KEY (ruc, version_id)
);
CREATE INDEX ruc_snapshot_version_ruc_idx ON ruc_snapshot(version_id, ruc);

CREATE TABLE manual_ruc_results (
	ruc TEXT PRIMARY KEY CHECK (length(ruc) = 13),
	result_json TEXT NOT NULL CHECK (json_valid(result_json)),
	source_id TEXT NOT NULL,
	source_url TEXT NOT NULL,
	source_period TEXT,
	observed_at TEXT NOT NULL,
	freshness TEXT NOT NULL CHECK (freshness IN ('live', 'snapshot', 'stale', 'unknown')),
	updated_by TEXT NOT NULL
);

CREATE TABLE source_sync_runs (
	run_id TEXT PRIMARY KEY,
	source_id TEXT NOT NULL REFERENCES source_catalog(source_id),
	province_code TEXT REFERENCES province_catalog(province_code),
	status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'validated', 'activated', 'failed', 'cancelled')),
	started_at TEXT NOT NULL,
	completed_at TEXT,
	source_period TEXT,
	row_count INTEGER NOT NULL DEFAULT 0,
	unique_ruc_count INTEGER NOT NULL DEFAULT 0,
	archive_bytes INTEGER,
	normalized_bytes INTEGER,
	rows_written INTEGER NOT NULL DEFAULT 0,
	detail TEXT
);
CREATE INDEX source_sync_runs_source_date_idx
	ON source_sync_runs(source_id, started_at);

CREATE TABLE audit_events (
	event_id TEXT PRIMARY KEY,
	actor_email TEXT NOT NULL,
	action TEXT NOT NULL,
	target_type TEXT NOT NULL,
	target_id TEXT,
	occurred_at TEXT NOT NULL,
	request_id TEXT,
	details_json TEXT CHECK (details_json IS NULL OR json_valid(details_json))
);
CREATE INDEX audit_events_date_idx ON audit_events(occurred_at);

CREATE TABLE integration_usage_daily (
	integration_id TEXT NOT NULL REFERENCES integrations(integration_id),
	usage_date TEXT NOT NULL,
	request_count INTEGER NOT NULL DEFAULT 0,
	error_count INTEGER NOT NULL DEFAULT 0,
	d1_rows_read INTEGER NOT NULL DEFAULT 0,
	d1_rows_written INTEGER NOT NULL DEFAULT 0,
	sri_live_calls INTEGER NOT NULL DEFAULT 0,
	updated_at TEXT NOT NULL,
	PRIMARY KEY (integration_id, usage_date)
);

INSERT INTO province_catalog (province_code, province_name, enabled, updated_at)
VALUES ('EC-P', 'Pichincha', 0, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

INSERT INTO source_catalog (source_id, authority, province_code, source_url, enabled, status, created_at, updated_at)
VALUES (
	'sri_ruc_pichincha',
	'sri',
	'EC-P',
	'https://descargas.sri.gob.ec/download/datosAbiertos/SRI_RUC_Pichincha.zip',
	0,
	'pending',
	strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
	strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
);

INSERT INTO source_catalog (source_id, authority, province_code, source_url, enabled, status, created_at, updated_at)
VALUES (
	'sri_ruc_manual_live',
	'sri',
	NULL,
	'https://srienlinea.sri.gob.ec/facturacion-internet/consultas/publico/ruc-datos2.jspa',
	1,
	'ready',
	strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
	strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
);

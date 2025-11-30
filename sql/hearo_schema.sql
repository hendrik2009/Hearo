PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tags (
    uid             TEXT PRIMARY KEY,                 -- TagUID (NFC UID, hex string)
    playlist_uri    TEXT NOT NULL,                    -- Spotify playlist URI
    last_track_uri  TEXT NOT NULL DEFAULT '',         -- Last played track/episode URI
    last_pos_ms     INTEGER NOT NULL DEFAULT 0,       -- Last playback position (ms)
    updated_at      INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_tags_playlist_uri
    ON tags(playlist_uri);

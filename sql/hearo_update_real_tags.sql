BEGIN TRANSACTION;

-- Real Tag 1
INSERT INTO tags (uid, playlist_uri, last_track_uri, last_pos_ms)
VALUES ('A237CDC6',
        'spotify:playlist:5a8zB7HP4rN0xJvuWgq4oG',
        '',
        0)
ON CONFLICT(uid) DO UPDATE SET
    playlist_uri   = excluded.playlist_uri,
    last_track_uri = excluded.last_track_uri,
    last_pos_ms    = excluded.last_pos_ms,
    updated_at     = strftime('%s','now');

-- Real Tag 2
INSERT INTO tags (uid, playlist_uri, last_track_uri, last_pos_ms)
VALUES ('F269CFC6',
        'spotify:playlist:6oLjhkE1bXPTmEZp0YWHX0',
        '',
        0)
ON CONFLICT(uid) DO UPDATE SET
    playlist_uri   = excluded.playlist_uri,
    last_track_uri = excluded.last_track_uri,
    last_pos_ms    = excluded.last_pos_ms,
    updated_at     = strftime('%s','now');

-- Real Tag 3
INSERT INTO tags (uid, playlist_uri, last_track_uri, last_pos_ms)
VALUES ('50EE5F61',
        'spotify:playlist:0OOGCAvOLE8Cb15Ol05ad3',
        '',
        0)
ON CONFLICT(uid) DO UPDATE SET
    playlist_uri   = excluded.playlist_uri,
    last_track_uri = excluded.last_track_uri,
    last_pos_ms    = excluded.last_pos_ms,
    updated_at     = strftime('%s','now');

COMMIT;

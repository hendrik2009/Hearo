BEGIN TRANSACTION;

INSERT INTO tags (uid, playlist_uri, last_track_uri, last_pos_ms)
VALUES
    ('04AABBCCDD01',
     'spotify:playlist:TO_REPLACE_WITH_REAL_PLAYLIST_ID_1',
     'spotify:track:TO_REPLACE_WITH_REAL_TRACK_ID_1',
     300000),

    ('04AABBCCDD02',
     'spotify:playlist:TO_REPLACE_WITH_REAL_PLAYLIST_ID_2',
     '',
     0),

    ('04AABBCCDD03',
     'spotify:playlist:TO_REPLACE_WITH_REAL_PLAYLIST_ID_3',
     'spotify:episode:TO_REPLACE_WITH_REAL_EPISODE_ID_1',
     820000);

COMMIT;

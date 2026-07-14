UPDATE albums AS a
JOIN mfu_event AS e ON e.id = a.event_id
SET a.album_name = CASE
    WHEN e.starts_at IS NULL THEN CONCAT('【イベント】　', TRIM(e.title))
    ELSE CONCAT(
        '【イベント】　',
        DATE_FORMAT(e.starts_at, '%Y年%m月%d日'),
        '　',
        TRIM(e.title)
    )
END
WHERE a.access_mode = 'event';

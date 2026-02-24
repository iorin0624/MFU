-- 加工回し通知 調査用SQL
-- 前提: album_process(ext_user_id, album_id, child_id, request_by, request_flag, complete_flag)

-- 1) album_id + request_by 単位の集計
--    request_flag=1 の総数 / complete_flag=0 の残数 / complete_flag=1 の完了数
SELECT
  ap.album_id,
  ap.child_id,
  ap.request_by,
  SUM(CASE WHEN ap.request_flag = 1 THEN 1 ELSE 0 END) AS requested_total,
  SUM(CASE WHEN ap.request_flag = 1 AND ap.complete_flag = 0 THEN 1 ELSE 0 END) AS remaining_incomplete,
  SUM(CASE WHEN ap.request_flag = 1 AND ap.complete_flag = 1 THEN 1 ELSE 0 END) AS completed_total
FROM album_process ap
WHERE ap.album_id = :album_id
GROUP BY ap.album_id, ap.child_id, ap.request_by
ORDER BY ap.child_id, ap.request_by;

-- 2) 実際に process_done 通知対象として抽出される「未完了者一覧」
--    実装の抽出条件: request_flag=1 AND complete_flag=0
SELECT
  ap.album_id,
  ap.child_id,
  ap.ext_user_id,
  ap.request_by,
  ap.request_flag,
  ap.complete_flag,
  u.email
FROM album_process ap
LEFT JOIN external_login_user u ON u.id = ap.ext_user_id
WHERE ap.album_id = :album_id
  AND ap.child_id = :child_id
  AND ap.request_flag = 1
  AND ap.complete_flag = 0
ORDER BY ap.ext_user_id;

-- 3) UI表示とDBズレ疑いを確認（完了扱いなのに未完了で残る/二重状態確認）
-- 3-a) 同一 child 内で request_flag=1 なのに complete_flag が 0 のまま長く残っている行
--      （updated_at がある場合は経過時間で疑い判定。無い場合はそのまま確認）
SELECT
  ap.album_id,
  ap.child_id,
  ap.ext_user_id,
  ap.request_by,
  ap.request_flag,
  ap.complete_flag,
  ap.updated_at,
  u.email
FROM album_process ap
LEFT JOIN external_login_user u ON u.id = ap.ext_user_id
WHERE ap.album_id = :album_id
  AND ap.child_id = :child_id
  AND ap.request_flag = 1
  AND ap.complete_flag = 0
ORDER BY ap.updated_at ASC, ap.ext_user_id ASC;

-- 3-b) request_flag=0 なのに complete_flag=1 の行（更新ロジックの不整合を疑う）
SELECT
  ap.album_id,
  ap.child_id,
  ap.ext_user_id,
  ap.request_by,
  ap.request_flag,
  ap.complete_flag,
  u.email
FROM album_process ap
LEFT JOIN external_login_user u ON u.id = ap.ext_user_id
WHERE ap.album_id = :album_id
  AND ap.child_id = :child_id
  AND ap.request_flag = 0
  AND ap.complete_flag = 1
ORDER BY ap.ext_user_id;

-- 4) ログに出る child_id と突合するための詳細
SELECT
  ap.*,
  u.email AS ext_user_email,
  req.email AS request_by_email
FROM album_process ap
LEFT JOIN external_login_user u ON u.id = ap.ext_user_id
LEFT JOIN external_login_user req ON req.id = ap.request_by
WHERE ap.album_id = :album_id
  AND ap.child_id = :child_id
ORDER BY ap.ext_user_id;

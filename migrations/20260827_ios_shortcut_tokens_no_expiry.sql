-- iPhoneショートカット用APIキーは管理画面で無効化するまで有効とする。
UPDATE uploader_tokens
   SET expires_at = NULL
 WHERE scope = 'ios_shortcut_upload';

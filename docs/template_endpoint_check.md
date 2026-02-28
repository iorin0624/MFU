# テンプレート endpoint 整合チェック

テンプレート内の `url_for('...')` 静的参照と Flask の実在 endpoint (`app.url_map`) を突合するため、以下を実行します。

```bash
python tools/check_template_endpoints.py
```

- デフォルトでリポジトリ配下の `*.html` を全走査します。
- `--template-dir` を指定すると、指定ディレクトリ配下のみに絞って走査できます。
- 未登録 endpoint が 1 件でもある場合は `exit 1` で終了します。
- Flask アプリ初期化に失敗した場合は `exit 2` で終了します。

ローカル確認例:

```bash
python tools/check_template_endpoints.py
flask routes | grep external_login_user
```

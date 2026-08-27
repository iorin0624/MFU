# NTP Scheduled Reload (Phase 1)

Raspberry Pi / Chronyの基準時刻を使い、指定したChromeタブを指定日時にリロードするManifest V3拡張です。フォーム送信、購入確定、自動クリック、CAPTCHA回避は行いません。

## 対象環境とインストール

- 対象: Chrome 151
- `minimum_chrome_version`: 150（`chrome.alarms`の`persistAcrossSessions`利用のため）
- Chromeの「拡張機能を管理」から開発者モードを有効にし、「パッケージ化されていない拡張機能を読み込む」でこのディレクトリを指定します。
- Chromeへ登録済みの `dist/ntp_scheduled_reload_v1.0.1` を今後も同じ場所で上書きします。フォルダー名は維持しますが、実際の拡張機能バージョンはManifestに従います。以後は再選択せず、拡張機能画面の再読み込みだけで更新できます。

`activeTab`は使用していません。予約をChrome再起動後も実行できるよう、予約登録時にそのサイトのoriginだけを任意権限として要求します。`<all_urls>`を常時許可しません。

## Raspberry Pi API

Service Workerだけが `http://192.168.103.15:5055/chrony/time` を取得します。content scriptや対象ページからPi APIを直接取得しません。5回測定してRTT最小のサンプルを採用し、Leap status、Stratum、RTT、異常なoffset変化を検査します。

## 実行経路

1. 対象タブ、JST日時（ミリ秒）、実行補正を登録
2. T-120秒とT-30秒を別々の永続Alarmで事前起動
3. Service WorkerがPi APIを同期し、対象タブへrunnerを挿入
4. T-30秒で必要に応じて対象タブを前面化
5. T-5秒にrunnerからService Workerへメッセージを送り、Service WorkerがPi APIを再取得
6. runnerは`performance.now()`基準で段階待機し、最終20msだけ短時間スピン
7. `location.reload()`を呼び、推定実行誤差をログへ保存

Alarmは高精度タイマーとして使わず、runnerを起動する事前通知だけに使います。PCがスリープしている間は予定時刻に実行できません。

T-30以降に対象タブが同じページでreloadされた場合は`tabs.onUpdated`でrunnerを再挿入します。URLのorigin/pathが変わった場合、または実行直前で安全に再挿入できない場合は中止します。

## 実行補正

範囲は -1000～+5000msです。内部100回測定の結果、既定値は0msとしました。ネットワーク到達時刻を補正するものではありません。DNS、TCP/TLS、CDN、回線、相手サーバー負荷の遅延は別に発生します。

## テスト

- popupの「10秒後にテスト」「30秒後にテスト」は、本番と同じNTP同期→runner→reload経路を使います。
- 「精度テスト」は外部サイトを使わず、タイマー方式と最終20msスピン方式を各100回比較します。
- Node単体テスト: `node --test tests/unit.test.mjs`

実測値は [test/RESULTS.md](test/RESULTS.md) を参照してください。

## 制約

- 保証対象はChromeが`location.reload()`を呼び出す推定時刻までです。予約サイトへのHTTP到達時刻は保証しません。
- バックグラウンドタブのタイマー抑制を避けるため、前面化は初期ONです。
- Pi APIが使えない場合は既定で中止します。明示設定時のみPC時計で強行します。
- 待機列、一時トークン、CSRF、セッション等を持つサイトでは、リロードが不利になることがあります。

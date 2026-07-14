# MFU Download

MFUのWeb画面で選択したJPEGを、iOS／Androidの写真ライブラリにある
`iori0624`アルバムへ保存するFlutterアプリです。アプリへのログインはありません。

## 動作

1. Webの`/view/<uuid>`でJPEGを選択します。
2. `写真アプリに保存`を押します。
3. Universal Link／Android App Linkでこのアプリを開きます。
4. 1回限りの起動トークンをダウンロードセッションへ交換します。
5. JPEGを1枚ずつ取得し、`iori0624`アルバムへ保存します。

同じMFU画像IDは端末内の保存履歴でスキップします。保存前に
`保存済みの写真も再保存`を有効にすると再保存できます。

## Bundle ID

- iOS: `jp.iori0624.mfudownload`
- Android: `jp.iori0624.mfudownload`
- Custom URL scheme: `mfudownload://job?token=...`

## Android Build

Flutter SDKとAndroid Studio／Android SDKを導入し、FlutterをPATHへ追加します。

```bat
build_android.bat
```

APK:

```text
build\app\outputs\flutter-apk\app-release.apk
```

Google Playへ出す場合は、`android/app/build.gradle.kts`のrelease署名を
専用keystoreへ変更し、次を実行します。

```bash
flutter build appbundle --release
```

## iOS Build

Macへこのフォルダーをコピーし、Xcode、Flutter SDK、Apple Developerの
署名チームを設定します。

```bash
chmod +x build_ios.sh
./build_ios.sh
```

IPA:

```text
build/ios/ipa
```

## Server association settings

サーバーの`.env`に署名情報を設定します。

```dotenv
MFU_MOBILE_DOWNLOAD_ENABLED=1
MFU_IOS_TEAM_ID=Apple Developer Team ID
MFU_ANDROID_SHA256_FINGERPRINTS=AA:BB:CC:...
```

`MFU_MOBILE_DOWNLOAD_ENABLED`は、アプリの実機確認と配布準備が終わってから
`1`にします。未設定時はWebのアプリ保存ボタンを表示しません。

- iOS: `https://mfu.iori0624.jp/.well-known/apple-app-site-association`
- Android: `https://mfu.iori0624.jp/.well-known/assetlinks.json`

署名値を変更した後はサーバーを再起動してください。iOSではXcodeの
Signing & Capabilitiesで`Runner/Runner.entitlements`が選択されていることを確認します。

# MFU写真保存ショートカット

`MFU写真保存.xml` は、MFUの「SCでDL」から受け取った一回限りのURLを利用して、選択した写真・動画をiPhone/iPadの「写真」アプリへ保存するショートカットのソースです。

## Windowsで実施済みの範囲

- Shortcuts Playgroundの形式に沿ったXML生成
- XML/plist構文とアクション構成の静的検証
- MFUの既存モバイルダウンロードAPIに合わせた通信処理

## Macで行う作業

Appleの仕様により、ショートカットの署名と実機への読み込みはmacOSで行います。

1. [Shortcuts Playground](https://github.com/viticci/shortcuts-playground-plugin) を取得します。
2. `MFU写真保存.xml` をMacへコピーします。
3. Shortcuts Playground付属の署名スクリプトで署名します。
4. 生成された署名済みファイルをiPhone/iPadへ読み込みます。
5. MFUの「SCでDL」から、写真と動画の保存・完了履歴を確認します。

例:

```bash
./scripts/sign-shortcut.sh /path/to/MFU写真保存.xml /path/to/MFU写真保存.shortcut
```

署名前に、Shortcuts Playgroundの最新版に付属する検証スクリプトでも再確認してください。

```bash
python3 scripts/validate_shortcut.py /path/to/MFU写真保存.xml --target-platform all
```

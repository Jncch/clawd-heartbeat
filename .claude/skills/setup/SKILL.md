---
name: setup
description: >
  Interactive first-time setup for Clawd Heartbeat. Use when the user says
  "set this up", "setup", "install", "get it working", "flash it", asks what to do
  after cloning — or in Japanese:「セットアップして」「初期設定」「導入したい」
  「動かしたい」「書き込んで」. Walks through WiFi config → build & flash →
  mDNS check → hook installation → verification, one validated step at a time.
---

# Clawd Heartbeat セットアップ手順

各フェーズに検証があります。検証を飛ばして先に進まないこと(失敗箇所の切り分けができなくなる)。
ユーザーの言語(日本語/英語)に合わせて進めてください。

## 0. 前提確認

1. `pio --version` で PlatformIO Core CLI の有無を確認。なければ `brew install platformio`(macOS)等で導入
2. M5Atom Lite を USB 接続し、`ls /dev/tty.usb* /dev/ttyUSB* /dev/ttyACM*` でポートが見えるか確認
   - 見えない場合: 充電専用ケーブルが最頻出の原因。データ対応ケーブル(C to C 推奨)に交換してもらう。次点でドライバ(CH9102/CP210x)

## 1. WiFi 設定

1. `cp include/secrets.h.example include/secrets.h`
2. ユーザーに **2.4GHz の SSID とパスワード**を確認して記入(Atom Lite は 5GHz 非対応)。
   ユーザーが自分で編集したい場合はプレースホルダの場所を案内して待つ
3. secrets.h は gitignore 済みであることを伝える

## 2. ビルドと書き込み

```bash
pio run -t upload
```

- 初回は toolchain のダウンロードで数分かかる(異常ではない)
- `espressif32@6.9.0` の解決に失敗したら**ユーザーに報告**。勝手に最新版へ上げない
- 書き込みモードに入らない場合: Atom のボタン(LED 面)を押しながら USB を挿し直してもらう

**検証**: 書き込み成功後、LED が紫(WiFi 接続中)→ 青(idle)になること。
紫のままなら WiFi 未接続(SSID/パスワード/2.4GHz を再確認)。

## 3. 疎通確認(mDNS)

デバイスは mDNS で `atom.local` として名乗るので、**IP 固定(DHCP 予約)は不要**。

1. `curl -4 -s -m 3 http://atom.local/` が `state=...` を返すか確認。
   **`-4` は必須**: 付けないと macOS が AAAA の応答を待ち、ESP32 は返さないため毎回タイムアウトする
2. `echo "http://atom.local" > .atom-ip` を作成(gitignore 済み)
3. **検証**: `./led-test.sh status` が state 等を返すこと
4. `atom.local` が引けない場合(Linux で avahi/nss-mdns なし、ゲスト WiFi・AP アイソレーション等)は
   IP 直打ちにフォールバック: シリアルから IP を読み、`.atom-ip` と led.sh の `ATOM_URL` に IP を書く。
   この場合は IP が変わると止まるので、ルーターの DHCP 予約で固定してもらう(ユーザー操作)
   - シリアル読み取り: `pio device monitor` は TTY 必須で背景実行不可。pyserial でポートを
     `dtr=False` `rts=False` で開いて読む。IP は起動時に 1 回しか出ないので、開いた後に
     RTS を一瞬 True → False にしてリセットしてから読む
   - 約 20 秒ごとに `rst:0xc (SW_CPU_RESET)` が出て `ready:` が出ないなら WiFi 接続失敗による再起動ループ

## 4. hook 設定

1. `cp led.sh ~/.claude/led.sh && chmod +x ~/.claude/led.sh`。既定の宛先は `http://atom.local` なので
   通常は編集不要(手順 3 で IP フォールバックした場合のみ `ATOM_URL` を書き換える)
2. `~/.claude/settings.json` の `hooks` に README 記載の 10 イベントをマージする。
   **既存の settings.json を丸ごと上書きしない**こと。既に hooks キーがある場合は中身を統合。
   編集後は JSON の構文検証を行う
3. すべてのイベントで `"async": true` が付いていることを確認(付いていないと Claude Code がブロックされる)

## 5. 最終検証

1. Claude Code を再起動してもらい、`/hooks` で全イベントが読み込まれていることを確認
2. 適当なファイルを読ませて: 白の呼吸(作業中)→ 緑点滅 6 秒(完了)→ 青(idle)と遷移すること
3. 許可のいらないコマンドを何か実行させて権限ダイアログを出し、赤点滅になること
4. 全部通ったら完了。`docs/LIFECYCLE.md` に「Yes 押したのに赤いまま」等の既知挙動が
   あることを一言案内する

## トラブル時

README のトラブルシュート表と `docs/NOTES.md` を参照。デバイスの状態は
`./led-test.sh status` でセッション別に確認できる。

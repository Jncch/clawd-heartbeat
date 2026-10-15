# かんたんセットアップ(実験版)

PlatformIO も `secrets.h` の編集も不要で、**USB でつないでコマンド 1 つ**で
ファームウェア書き込み → 接続設定 → hook 設定まで終わらせる方式です。

接続方式は 2 つあります。

| 方式 | 経路 | 向いている環境 | 対応 OS |
| --- | --- | --- | --- |
| **WiFi** | hook → HTTP → Atom | 自宅など。Atom を PC から離して置ける | macOS / Linux |
| **USB** | hook → HTTP → bridge(PC に常駐)→ USB シリアル → Atom | 社内ネットワークなど、WiFi が使えない・端末同士が通信できない環境 | macOS / Windows / Linux |

Windows は USB モードのみです(WiFi モードは検証で接続まで確認できなかったため対象外)。Windows で WiFi 接続にしたい場合は、通常版の手順(PlatformIO + `secrets.h`)を使ってください。

> 通常版(リポジトリ直下の `src/` + `secrets.h`)とは独立しています。
> こちらのファームウェアを書き込むと、Atom の中身は easy-setup 版に置き換わります。

## 手順

### 1. uv を入れる(初回のみ)

uv が Python と必要なライブラリを自動で用意します。Python のインストールは不要です。

macOS / Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows(PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. Atom を USB でつないで実行

```bash
cd clawd-heartbeat/easy-setup
uv run setup.py
```

あとは画面の質問に答えるだけです。

1. **接続方式** — WiFi / USB を選ぶ(`--mode wifi` / `--mode usb` で指定も可。Windows は質問せず USB)
2. **書き込み** — Atom を自動検出してファームウェアを書き込む
3. **接続設定**
   - WiFi: PC が今つないでいる WiFi の名前を取得し、保存済みのパスワードを PC から取り出す
     (macOS はキーチェーンの確認ダイアログ、Linux は NetworkManager)。取り出せなければ手入力。
     その後 `atom.local` で届くかを確認し、届かなければ IP を使う
   - USB: Atom を USB モードに切り替え、中継プログラム(bridge)を PC に常駐させる
4. **hook** — `~/.claude/led.sh` を配置し、`~/.claude/settings.json` に hook を追加する
   (既存の設定は残し、変更前にバックアップを作る)。`led.sh` の宛先は方式に合わせて書き換える

最後に Claude Code を再起動すれば完了です。

## WiFi を変えたとき

```bash
uv run setup.py --wifi-only
```

## USB モードの bridge について

`bridge.py` は `127.0.0.1:47801` で HTTP を受け、USB シリアルで Atom に中継する小さな常駐プログラムです。
API は Atom 本体と同じ(`/led` `/rgb` `/`)なので、`led.sh` は宛先を変えるだけでそのまま動きます。

- 置き場所: `~/.claude/clawd-heartbeat/bridge.py`(ログも同じ場所の `bridge.log`)
- 自動起動: macOS は launchd(`~/Library/LaunchAgents/com.clawd-heartbeat.bridge.plist`)、
  Windows はレジストリの Run(`HKCU\...\Run\ClawdHeartbeatBridge`)、Linux は systemd のユーザーサービス
- Atom を抜き差ししても自動で再接続する
- 動作確認: `ATOM=http://127.0.0.1:47801 ../led-test.sh status`
- 解除: `uv run setup.py --remove-bridge`

bridge はシリアルポートを開いたままにします(Atom はポートを開くたびにリセットされるため、hook ごとに開閉する方式は使えません)。
`setup.py` は実行時に bridge を一時停止してからポートを使います。

## LED の見かた(セットアップ中)

| LED | 意味 |
| --- | --- |
| 紫のゆっくり点滅 | WiFi 未設定(`setup.py` を待っている)。USB モードでは出ない |
| 紫の点灯 | WiFi に接続中 |
| 青 | 接続完了(待機) |

## うまくいかないとき

| 症状 | 対処 |
| --- | --- |
| Atom が見つからない | 充電専用ケーブルでないか確認。Windows は USB-シリアルのドライバが必要な場合あり(setup.py がドライバなしで認識されている機器を表示する)。FTDI 版(VID_0403)は Windows Update のオプションの更新プログラムか [FTDI 公式 VCP ドライバ](https://ftdichip.com/drivers/vcp-drivers/)、CH9102 版(VID_1A86)は WCH の CH343SER ドライバ |
| 書き込みに失敗する | Atom のボタン(LED 面)を押しながら USB を挿し直して再実行 |
| SSID が見つからない | Atom は 2.4GHz のみ対応。5GHz 専用の SSID は使えない |
| macOS で WiFi 名が自動で出ない | 最近の macOS は位置情報の許可がないと SSID を取得できない。手入力すれば、パスワードはキーチェーンから取得できる |
| PC から Atom に届かない | ゲスト WiFi や AP アイソレーションのある環境では端末同士が通信できない。USB モードを使う |
| USB モードで LED が変わらない | `~/.claude/clawd-heartbeat/bridge.log` を確認。`curl http://127.0.0.1:47801/` が 503 なら Atom が見つかっていない |

## オプション

| オプション | 内容 |
| --- | --- |
| `--mode wifi\|usb` | 接続方式を指定する(省略時は質問) |
| `--port <ポート>` | ポートを指定する(自動検出がうまくいかないとき) |
| `--wifi-only` | WiFi だけ設定し直す |
| `--no-hooks` | hook 設定をしない |
| `--claude-dir <dir>` | hook と bridge の設定先(既定: `~/.claude`) |
| `--remove-bridge` | USB モードの bridge を停止し、自動起動を解除する |

## 開発者向け

`dist/clawd-heartbeat.bin` は、ブートローダ・パーティション・アプリを結合した配布用イメージです(オフセット 0x0 に書き込む)。ファームウェアを変更したら、次のコマンドで作り直します(PlatformIO が必要)。

```bash
uv run setup.py --build
```

WiFi 設定は NVS に保存され、ビルドには含まれません。そのため `dist/` の bin はそのまま配布できます。

### 通常版ファームウェアとの違い

`firmware/src/main.cpp` は直下の `src/main.cpp` のコピーで、違いは WiFi まわりだけです。

- `secrets.h` を使わず、NVS(`Preferences`)から SSID/パスワードを読む
- WiFi 接続を非ブロッキングにした(接続待ち・失敗中もシリアルコマンドを受け付けるため)。
  接続に失敗しても再起動はせず、20 秒ごとに再試行する
- 接続方式(`mode`)も NVS に保存する。USB モードでは WiFi を切る
- USB シリアルのコマンド(115200bps、1 行 1 コマンド):

| コマンド | 応答 |
| --- | --- |
| `CLAWD?` | `CLAWD 1 usb` / `CLAWD 1 unconfigured` / `CLAWD 1 connecting` / `CLAWD 1 connected <IP>` |
| `WIFI <SSID の16進> <パスワードの16進>` | WiFi モードにする。`OK wifi saved` → `ready: http://<IP>` または `wifi: failed status=<N>`。パスワードなしは `-` |
| `MODE usb` | USB モードにする。`OK usb mode` |
| `FORGET` | 設定をすべて消去する。`OK forgotten` |
| `LED <state> <sid> <ts>` | `/led` と同じ(成功時は応答なし) |
| `RGB <r> <g> <b>` | `/rgb` と同じ(成功時は応答なし) |
| `STATUS` | `GET /` と同じ本文のあとに `END` |

LED の色・hook の挙動・HTTP API は通常版と同じです。`src/main.cpp` の LED まわりを変更したら、こちらにも反映してください。

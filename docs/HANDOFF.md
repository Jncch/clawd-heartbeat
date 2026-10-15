# M5Atom Lite で Claude Code のステータスを LED 表示する — 引き継ぎ資料

このドキュメントの読み手は Claude Code です。ユーザーの環境で実際に手を動かして構築してもらうことを想定しています。

**先に読むべき前提**: 設計の議論はすでに終わっており、方式は決定済みです。セクション 3 の「採用しなかった案」を読んでから、方式の再検討を提案してください。理由を知らずに「シリアルの方が簡単では」と言うと、既に踏んだ地雷を踏み直すことになります。

---

## 1. 作るもの

Claude Code の実行状態を、机の上に置いた M5Atom Lite の LED 1 粒で表示します。ターミナルを見ていなくても「今動いている」「承認待ちで止まっている」「終わった」が視界の端でわかる、というのが目的です。

特に重要なのは **承認待ちの検知**です。Claude Code が権限確認ダイアログで止まっているのに気づかず放置する、という状況をなくすことが動機になっています。

## 2. ハードウェア

| 項目 | 内容 |
| :--- | :--- |
| 本体 | M5Atom Lite（ESP32-PICO-D4） |
| LED | オンボード SK6812（WS2812 互換）× 1、**GPIO27** |
| 給電 | USB Type-C（常時給電前提。電池運用は考えない） |
| USB-UART | CH9102F または CP2104（ロットによる） |
| 追加部品 | **なし**。抵抗もコンデンサも外付け LED も不要 |

拡張が必要になった場合、Grove ポート（GPIO26 / GPIO32、5V 出力あり）に WS2812B ストリップを足せます。今回のスコープ外。

## 3. アーキテクチャ

### 採用: WiFi + HTTP

Atom Lite に `WebServer` を立て、Claude Code の hook から HTTP で状態を投げます。

```
Claude Code hook ──HTTP GET──> Atom Lite (WebServer:80) ──> FastLED ──> SK6812
```

### 採用しなかった案とその理由

**シリアル直叩き（hook から `echo "t" > /dev/tty.usbserial-*`）** — Atom Lite は USB-UART 変換チップ経由で、**DTR/RTS が EN/IO0 に接続されているため、ホストがシリアルポートを開くたびに自動リセットが走ります**。hook が発火するたびにボードが再起動し、300ms 前後の起動待ちが入った上で LED 状態が消えます。ステータスインジケータとして成立しません。

この問題は Pro Micro（ネイティブ USB CDC）では起きなかったもので、Atom Lite に移行したことで初めて表面化したものです。**シリアル方式に戻すことを提案しないでください。**

**常駐デーモン（pyserial でポートを開きっぱなしにして Unix socket 経由）** — 上記リセット問題は回避できますが、常駐プロセスの管理が必要になり、かつ Atom Lite が USB ケーブルの届く範囲にしか置けません。WiFi 方式なら USB 電源さえあれば机の見やすい位置に置けるため、実用上こちらが優位と判断しました。

## 4. 状態の定義

| 状態 | 見た目 | 発火元 |
| :--- | :--- | :--- |
| `idle` | ごく暗い青（常時点灯） | SessionStart / SessionEnd / 各状態からの自動復帰 |
| `tool` | 黄の点滅（250ms 周期） | UserPromptSubmit / PreToolUse |
| `wait` | 赤のゆっくりした呼吸 | Notification（`permission_prompt` のみ） |
| `done` | 緑点灯 3 秒 → idle へ自動復帰 | Stop |
| `err` | 赤の高速点滅（120ms） | StopFailure |

`PostToolUse` は意図的に使っていません。ツール完了直後は Claude が思考中であり、状態としては `tool` のままが正しいためです。次の `PreToolUse` か `Stop` が上書きします。

---

## 5. 作業手順

各フェーズに検証ステップがあります。**検証を飛ばさないでください。** まとめて作ってから動かすと、配線・ライブラリ・WiFi・hook のどこで失敗しているのか切り分けられなくなります。

### フェーズ 0: 環境確認

1. USB ケーブルが**データ通信対応**か確認（充電専用ケーブルでの詰まりが多い）
2. Atom Lite を接続し、シリアルポートが見えるか確認
   - macOS / Linux: `ls /dev/tty.* /dev/ttyUSB* /dev/ttyACM* 2>/dev/null`
   - 見えない場合: macOS は WCH の CH9102 ドライバ、Linux は `sudo usermod -aG dialout $USER` の後に再ログイン
3. VS Code + PlatformIO IDE 拡張をインストール（初回は toolchain のダウンロードで数分かかる）

> Arduino IDE 2.x を使う場合は、ボードマネージャ URL に `https://espressif.github.io/arduino-esp32/package_esp32_index.json` を追加 → esp32 by Espressif Systems をインストール → ボード「M5Stack-ATOM」を選択 → ライブラリマネージャで FastLED。スケッチは共通です。

**検証**: シリアルポートが `ls` で見えること。

### フェーズ 1: プロジェクト作成

PIO Home → New Project → Board: `M5Stack-ATOM`、Framework: Arduino。

`platformio.ini`:

```ini
[env:m5stack-atom]
platform = espressif32@6.9.0
board = m5stack-atom
framework = arduino
monitor_speed = 115200
lib_deps = fastled/FastLED@^3.6.0
```

**platform をバージョン固定している理由**: Espressif は PlatformIO 公式プラットフォームの保守を終了しており、最新を取りに行くと ESP32 Arduino core 3.x 系との噛み合わせでビルドが通らないことがあります。Atom Lite は旧 ESP32 なので 6.9.0 で機能的に不足はありません。**もし 6.9.0 の解決に失敗する場合はユーザーに報告し、勝手に最新版に上げないでください。**

`M5Atom` / `M5Unified` ライブラリは使いません。LED API の場所がバージョンで揺れる上、依存として結局 FastLED を引くためです。FastLED を直接使ったほうが読みやすく、`GRB` テンプレート引数が色順を吸収してくれるので `CRGB::Yellow` がそのまま黄色になります。

### フェーズ 2: 点灯だけ通す

WiFi を足す前に、書き込み経路が生きていることを確認します。

```cpp
#include <FastLED.h>
CRGB leds[1];

void setup() {
  FastLED.addLeds<WS2812B, 27, GRB>(leds, 1);
  FastLED.setBrightness(40);
}

void loop() {
  leds[0] = CRGB::Red;   FastLED.show(); delay(500);
  leds[0] = CRGB::Green; FastLED.show(); delay(500);
  leds[0] = CRGB::Blue;  FastLED.show(); delay(500);
}
```

**検証**: 赤 → 緑 → 青の順に点灯すること。順序が狂う場合は `GRB` を `RGB` に変更して再確認。

書き込みに失敗する場合は、**Atom Lite のボタン（LED 部分そのものが押せます）を押しながら USB を挿す**とダウンロードモードに入ります。

### フェーズ 3: 本体ファームウェア

セクション 6 のコードを書き込みます。`WIFI_SSID` / `WIFI_PASS` はユーザーに確認してください（セクション 9）。

**検証**: シリアルモニタに IP アドレスが出力され、LED が暗い青になること。紫のまま止まっている場合は WiFi 接続待ちです（セクション 8 参照）。

### フェーズ 4: IP 固定と疎通確認

`atom.local`（mDNS）は Linux では avahi が必要で、環境によっては解決に数百 ms かかります。**ルーターの DHCP 予約で Atom の MAC に固定 IP を割り当て、hook からは IP 直打ち**にしてください。mDNS はブラウザから様子を見る用の保険として残してあります。

```bash
curl "http://192.168.1.50/led?s=wait"   # → 赤が呼吸を始める
curl "http://192.168.1.50/led?s=done"   # → 緑3秒 → 暗い青
curl "http://192.168.1.50/"             # → 現在の状態を返す
```

**検証**: 上記 3 つがすべて期待通りに動くこと。ここが通らないうちに hook 設定に進まないでください。

### フェーズ 5: hook 設定

セクション 7 の内容を配置します。

**検証**: Claude Code を再起動し、`/hooks` で各イベントが読み込まれていることを確認。その後、適当なファイルを読ませて `黄点滅 → 緑3秒 → 暗い青` と遷移すること。承認が必要な操作を投げて赤の呼吸になること。

---

## 6. ファームウェア

```cpp
#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <FastLED.h>

// ---- ユーザー設定 ----
const char* WIFI_SSID = "＿＿＿＿";
const char* WIFI_PASS = "＿＿＿＿";
const char* HOSTNAME  = "atom";
const uint8_t BRIGHTNESS = 40;      // 30〜50 推奨。素の明るさは目に痛い
// ----------------------

#define LED_PIN 27
#define STALE_MS (10UL * 60UL * 1000UL)   // 10分更新がなければ idle に落とす

CRGB leds[1];
WebServer server(80);

enum State { IDLE, TOOL, WAIT, DONE, ERR };
State state = IDLE;
unsigned long stateSince = 0;

void setState(State s) {
  state = s;
  stateSince = millis();
}

void handleLed() {
  String s = server.arg("s");
  if      (s == "tool") setState(TOOL);
  else if (s == "wait") setState(WAIT);
  else if (s == "done") setState(DONE);
  else if (s == "err")  setState(ERR);
  else if (s == "idle") setState(IDLE);
  else { server.send(400, "text/plain", "unknown state\n"); return; }
  server.send(200, "text/plain", "ok\n");
}

void render() {
  unsigned long t = millis() - stateSince;
  CRGB c = CRGB::Black;

  // 取りこぼし対策: 長時間 tool/wait のままなら idle に自動復帰
  if ((state == TOOL || state == WAIT) && t > STALE_MS) {
    setState(IDLE);
    t = 0;
  }

  switch (state) {
    case IDLE:
      c = CHSV(160, 255, 12);                          // ごく暗い青
      break;
    case TOOL:
      c = ((t / 250) % 2) ? CRGB::Yellow : CRGB::Black;
      break;
    case WAIT:
      c = CHSV(0, 255, beatsin8(30, 20, 255));         // 赤の呼吸
      break;
    case DONE:
      if (t > 3000) { setState(IDLE); return; }
      c = CRGB::Green;
      break;
    case ERR:
      c = ((t / 120) % 2) ? CRGB::Red : CRGB::Black;
      break;
  }
  leds[0] = c;
  FastLED.show();
}

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);          // モデムスリープ無効化（応答遅延の抑制）
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(200);
  }
}

void setup() {
  Serial.begin(115200);
  FastLED.addLeds<WS2812B, LED_PIN, GRB>(leds, 1);
  FastLED.setBrightness(BRIGHTNESS);
  leds[0] = CRGB::Purple; FastLED.show();   // 接続中の目印

  connectWiFi();
  if (WiFi.status() != WL_CONNECTED) {
    // 20秒で繋がらなければ再起動してやり直す
    ESP.restart();
  }

  MDNS.begin(HOSTNAME);
  MDNS.addService("http", "tcp", 80);

  server.on("/led", handleLed);
  server.on("/", []() {
    const char* names[] = {"idle", "tool", "wait", "done", "err"};
    server.send(200, "text/plain",
      String("state=") + names[(int)state] +
      "\nuptime=" + String(millis() / 1000) + "s"
      "\nrssi=" + String(WiFi.RSSI()) + "\n");
  });
  server.begin();

  Serial.print("ready: http://");
  Serial.println(WiFi.localIP());
  setState(IDLE);
}

void loop() {
  server.handleClient();
  render();

  // WiFi 断の復旧（数週間置きっぱなしにする前提）
  static unsigned long lastCheck = 0;
  if (millis() - lastCheck > 30000) {
    lastCheck = millis();
    if (WiFi.status() != WL_CONNECTED) ESP.restart();
  }
}
```

### 設計上の要点

**`WiFi.setSleep(false)`** — これがないと ESP32 のモデムスリープが効き、リクエスト応答に 100〜200ms のばらつきが出ます。インジケータとして反応が鈍く感じます。常時 USB 給電なので消費電力を気にする理由はありません。

**`loop()` に `delay()` を入れない** — 点滅はすべて `millis()` 差分で描画しています。`delay` を混ぜた瞬間に `handleClient()` が止まり、レスポンスが遅れます。**この方針を崩す変更を加えないでください。**

**`STALE_MS` による自動復帰** — hook の取りこぼしや Claude Code の異常終了で `tool` / `wait` に張り付くのを防ぐ保険です。

**明るさ 40/255** — Atom Lite の SK6812 は素の明るさだと直視できません。机上運用では 30〜50 が上限です。

---

## 7. hook 設定

### `~/.claude/led.sh`

```bash
#!/usr/bin/env bash
exec curl -s -m 1 "http://192.168.1.50/led?s=$1" >/dev/null 2>&1
```

```bash
chmod +x ~/.claude/led.sh
```

`-m 1`（1 秒でタイムアウト）は必須です。Atom の電源が抜けている状態で curl が待つと、そのぶん hook プロセスが残ります。

### `~/.claude/settings.json`

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "$HOME/.claude/led.sh idle", "async": true }] }
    ],
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "$HOME/.claude/led.sh tool", "async": true }] }
    ],
    "PreToolUse": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "$HOME/.claude/led.sh tool", "async": true }] }
    ],
    "Notification": [
      { "matcher": "permission_prompt", "hooks": [{ "type": "command", "command": "$HOME/.claude/led.sh wait", "async": true }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "$HOME/.claude/led.sh done", "async": true }] }
    ],
    "StopFailure": [
      { "hooks": [{ "type": "command", "command": "$HOME/.claude/led.sh err", "async": true }] }
    ],
    "SessionEnd": [
      { "hooks": [{ "type": "command", "command": "$HOME/.claude/led.sh idle", "async": true }] }
    ]
  }
}
```

すでに `~/.claude/settings.json` が存在する場合は、`hooks` キーの中身をマージしてください。丸ごと上書きしないこと。

### なぜこの構成なのか

**`"async": true`** — hook は通常 Claude Code の実行をブロックします。`async` を付けるとバックグラウンドで実行され、ブロックしません。LED 更新は結果を待つ必要がないので、全イベントで付けるのが正解です。

**`Notification` の `"matcher": "permission_prompt"`** — `Notification` イベントは権限確認以外にも、入力アイドル（`idle_prompt`）や認証成功（`auth_success`）でも発火します。matcher で `permission_prompt` に絞ることで、「放置していたら勝手に赤くなる」を防いでいます。

> 補足: この matcher は比較的新しい機能です。ユーザーの Claude Code のバージョンが古く matcher が効かない場合は、hook 側で stdin の JSON を読み `.message` フィールドで分岐する方式にフォールバックしてください。

**`StopFailure`** — API エラーでターンが終了したときに発火します。`err`（赤の高速点滅）に割り当てています。matcher で `rate_limit` や `overloaded` を分けることもできますが、LED 1 粒では区別できないので分けていません。

---

## 8. 既知の落とし穴

躓きやすい順に並べています。

| 症状 | 原因と対処 |
| :--- | :--- |
| 書き込みモードに入らない | **ボタンを押しながら USB を挿す** |
| LED が紫のまま | WiFi 未接続。**Atom Lite は 2.4GHz のみ**。5GHz 専用 SSID には繋がりません |
| ポートが見えない | 充電専用ケーブル、またはドライバ未インストール（CH9102 / CP210x） |
| 色が違う | `GRB` ↔ `RGB` の並び順。フェーズ 2 で確認済みのはず |
| hook が発火しない | `/hooks` で読み込みを確認。JSON の構文エラーで丸ごと無視されることがある |
| `~` が展開されない | `$HOME` を使う（上記 JSON はすでに `$HOME`） |
| Claude Code が重い | `async: true` が抜けている、または `-m 1` がない |
| 承認待ちでないのに赤 | `Notification` の matcher が効いていない（バージョン確認） |

## 9. 作業開始前にユーザーへ確認すること

以下は資料に埋められていません。**推測で埋めずに聞いてください。**

1. **WiFi の SSID とパスワード**（2.4GHz のもの）
2. **Atom Lite に割り当てる固定 IP**（ルーターで DHCP 予約を設定してもらう。資料内の `192.168.1.50` は仮の値）
3. **開発環境**: PlatformIO でよいか、Arduino IDE を使いたいか
4. **hook の適用範囲**: `~/.claude/settings.json`（全プロジェクト）でよいか、特定プロジェクトのみか
5. **既存の `settings.json`** があるか（マージが必要か）

## 10. 参考

- Claude Code hooks リファレンス: https://code.claude.com/docs/en/hooks
  - イベント一覧、matcher の値、`async` / `timeout` などのフィールド定義
- FastLED: https://github.com/FastLED/FastLED
- M5Atom Lite ピンアサイン: LED = GPIO27、ボタン = GPIO39、Grove = GPIO26 / GPIO32

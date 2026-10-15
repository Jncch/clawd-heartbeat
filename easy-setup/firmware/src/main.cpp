#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <FastLED.h>
#include <Preferences.h>

// ---- ユーザー設定 ----
// WiFi 設定は NVS に保存(USB シリアル経由で setup.py が書き込む。secrets.h は使わない)
const char* HOSTNAME  = "atom";
const char* FW_PROTO  = "1";        // シリアル設定プロトコルの版(setup.py が照合する)
const uint8_t BRIGHTNESS = 255;     // ケース(拡散シェード)前提で最大。裸運用なら 30〜50 に戻す
// ----------------------

#define LED_PIN 27
#define BTN_PIN 39                        // 本体前面ボタン(押下で LOW)
#define RAINBOW_MS 10000UL                // ボタン押下でレインボー表示する時間
#define STALE_MS (10UL * 60UL * 1000UL)   // 10分更新がないセッションは失効
#define DEFAULT_STALE_MS (2UL * 60UL * 1000UL) // 手動送信(sid=default)は 2分で失効(テストの残留対策)
#define OFF_MS   (30UL * 60UL * 1000UL)   // 30分リクエストがなければ消灯
#define DONE_MS  6000UL                   // done の表示時間
#define WAIT_BLINK_MS 30000UL             // wait の点滅時間(以降は常時点灯で 10 分待って idle へ)
#define MAX_SESSIONS 8
#define WIFI_TIMEOUT_MS 20000UL           // この時間で繋がらなければ失敗を報告して再試行

CRGB leds[1];
WebServer server(80);
Preferences prefs;

// WiFi は非ブロッキングで接続する(接続待ち・失敗中もシリアル設定を受け付けるため)
enum Net { NET_UNCONFIGURED, NET_CONNECTING, NET_CONNECTED };
Net net = NET_UNCONFIGURED;
unsigned long netSince = 0;       // 接続試行の開始時刻
bool serverStarted = false;       // WebServer/mDNS は初回接続時に起動
String wifiSsid, wifiPass;
bool usbMode = false;             // true: WiFi を使わずシリアルで状態を受ける(NVS の mode=usb)

enum State { IDLE, TOOL, WAIT, DONE, ERR };

// 複数の Claude Code セッションが同時に叩いても上書きし合わないよう、
// セッション別に状態を持ち、表示は優先度(wait > err > done > tool > idle)で集約する
struct Session {
  char id[48];
  State st;
  unsigned long since;   // この状態になった時刻(done の失効判定用)
  unsigned long seen;    // 最終更新時刻(セッション失効判定用)
  uint64_t lastTs;       // 最後に適用した送信タイムスタンプ(順序逆転の排除用)
  bool used;
};
Session sessions[MAX_SESSIONS];

State shown = IDLE;               // 現在表示中の集約状態
unsigned long shownSince = 0;     // 点滅の位相基準
unsigned long waitSince = 0;      // 最新の wait 開始時刻(点滅→常灯の切替基準)
unsigned long lastRequest = 0;

bool rawActive = false;           // 発光テスト(/rgb)の直接制御中
CRGB rawColor = CRGB::Black;
unsigned long rawSince = 0;

bool rainbowActive = false;       // ボタン押下によるレインボー表示中
unsigned long rainbowSince = 0;   // レインボー開始時刻

Session* findSlot(const String& sid) {
  for (auto& s : sessions) if (s.used && sid == s.id) return &s;
  for (auto& s : sessions) if (!s.used) return &s;
  Session* oldest = &sessions[0];
  for (auto& s : sessions) if (s.seen < oldest->seen) oldest = &s;
  return oldest;
}

// 状態更新の本体(HTTP の /led と シリアルの LED コマンドで共用)。戻り値は応答文字列、nullptr は不正な状態名
const char* applyState(const String& s, String sid, const String& tsArg) {
  State st;
  if      (s == "tool") st = TOOL;
  else if (s == "wait") st = WAIT;
  else if (s == "done") st = DONE;
  else if (s == "err")  st = ERR;
  else if (s == "idle") st = IDLE;
  else return nullptr;

  if (sid.length() == 0) sid = "default";

  unsigned long now = millis();
  Session* slot = findSlot(sid);
  if (!slot->used || sid != slot->id) {
    strncpy(slot->id, sid.c_str(), sizeof(slot->id) - 1);
    slot->id[sizeof(slot->id) - 1] = 0;
    slot->used = true;
    slot->st = IDLE;
    slot->since = now;
    slot->lastTs = 0;
  }

  // async hook + curl リトライで届く順序が逆転することがあるため、
  // 送信タイムスタンプ(ts)が前回適用分より古い更新は捨てる(ts なしは常に適用)
  uint64_t ts = strtoull(tsArg.c_str(), nullptr, 10);
  if (ts > 0 && ts < slot->lastTs) {
    slot->seen = now;   // セッションは生きているので失効タイマーだけ更新
    return "stale";
  }
  if (ts > 0) slot->lastTs = ts;

  if (slot->st != st) slot->since = now;
  slot->st = st;
  slot->seen = now;

  rawActive = false;
  lastRequest = now;
  return "ok";
}

void handleLed() {
  const char* r = applyState(server.arg("s"), server.arg("sid"), server.arg("ts"));
  if (!r) { server.send(400, "text/plain", "unknown state\n"); return; }
  server.send(200, "text/plain", String(r) + "\n");
}

// 発光テスト用: /rgb?r=255&g=0&b=0 で任意色を直接点灯(次の /led で通常動作に戻る)
void applyRgb(int r, int g, int b) {
  rawColor = CRGB(r, g, b);
  rawActive = true;
  rawSince = millis();
  lastRequest = rawSince;
}

void handleRgb() {
  applyRgb(server.arg("r").toInt(), server.arg("g").toInt(), server.arg("b").toInt());
  server.send(200, "text/plain", "ok\n");
}

unsigned long staleFor(const Session& s) {
  return (strcmp(s.id, "default") == 0) ? DEFAULT_STALE_MS : STALE_MS;
}

State aggregate(unsigned long now) {
  bool anyWait = false, anyErr = false, anyDone = false, anyTool = false;
  for (auto& s : sessions) {
    if (!s.used || now - s.seen > staleFor(s)) continue;
    State st = s.st;
    if (st == DONE && now - s.since > DONE_MS) st = IDLE;
    if (st == WAIT && s.since > waitSince) waitSince = s.since;
    if      (st == WAIT) anyWait = true;
    else if (st == ERR)  anyErr  = true;
    else if (st == DONE) anyDone = true;
    else if (st == TOOL) anyTool = true;
  }
  if (anyWait) return WAIT;
  if (anyErr)  return ERR;
  if (anyDone) return DONE;
  if (anyTool) return TOOL;
  return IDLE;
}

int activeSessions(unsigned long now) {
  int n = 0;
  for (auto& s : sessions) if (s.used && now - s.seen <= staleFor(s)) n++;
  return n;
}

void render() {
  unsigned long now = millis();

  // ボタン押下によるレインボー(10 秒): 何よりも優先して表示
  // 減算で判定(millis() オーバーフロー対策。絶対値比較だと 49.7 日周回後に誤発動する)
  if (rainbowActive) {
    if (now - rainbowSince < RAINBOW_MS) {
      leds[0] = CHSV((uint8_t)(now / 8), 255, 255);   // 約 2 秒で色相一周
      FastLED.show();
      return;
    }
    rainbowActive = false;
  }

  // WiFi 未接続: 紫の常灯 = 接続中、紫のゆっくり点滅 = 未設定(USB で setup.py を待っている)
  if (!usbMode && net != NET_CONNECTED) {
    if (net == NET_CONNECTING) leds[0] = CRGB::Purple;
    else leds[0] = ((now / 1000) % 2) ? CRGB::Purple : CRGB::Black;
    FastLED.show();
    return;
  }

  // 30分リクエストがなければ消灯(次のリクエストで復帰)
  if (now - lastRequest > OFF_MS) {
    leds[0] = CRGB::Black;
    FastLED.show();
    return;
  }

  if (rawActive) {
    if (now - rawSince > STALE_MS) rawActive = false;
    leds[0] = rawColor;
    FastLED.show();
    return;
  }

  State agg = aggregate(now);
  if (agg != shown) {
    shown = agg;
    shownSince = now;
  }
  unsigned long t = now - shownSince;
  CRGB c = CRGB::Black;

  switch (shown) {
    case IDLE:
      c = CHSV(160, 255, 128);                         // 青(フルの 1/2。ケース越し視認用)
      break;
    case TOOL:
      c = CRGB::White;                                 // 白の呼吸(オレンジケース越しでも赤と混同しない)
      c.nscale8(beatsin8(40, 10, 255));
      break;
    case WAIT:
      // 最初の 30 秒は点滅で気づかせ、以降は常灯(離席から戻ったとき用)
      if (now - waitSince < WAIT_BLINK_MS)
        c = ((t / 400) % 2) ? CRGB(170, 0, 0) : CRGB::Black;   // err(120ms)より遅い点滅で区別
      else
        c = CRGB(170, 0, 0);
      break;
    case DONE:
      c = ((t / 150) % 2) ? CRGB::Green : CRGB::Black;
      break;
    case ERR:
      c = ((t / 120) % 2) ? CRGB::Red : CRGB::Black;
      break;
  }
  leds[0] = c;
  FastLED.show();
}

// GET / とシリアルの STATUS で返す本文
String statusBody() {
  const char* names[] = {"idle", "tool", "wait", "done", "err"};
  unsigned long now = millis();
  String body = String("state=") + (rawActive ? "raw" : names[(int)aggregate(now)]) +
    "\nsessions=" + String(activeSessions(now)) +
    "\nuptime=" + String(now / 1000) + "s" +
    (usbMode ? String("\nlink=usb\n") : "\nrssi=" + String(WiFi.RSSI()) + "\n");
  for (auto& s : sessions) {
    if (!s.used || now - s.seen > staleFor(s)) continue;
    body += String("  ") + s.id + " " + names[(int)s.st] +
            " age=" + String((now - s.seen) / 1000) + "s\n";
  }
  return body;
}

void wifiInit() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);          // モデムスリープ無効化（応答遅延の抑制）
  WiFi.setAutoReconnect(true);
}

// USB モード: WiFi を使わず、PC 側の bridge.py からシリアルで状態を受け取る
void enterUsbMode() {
  usbMode = true;
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  lastRequest = millis();
  shownSince = millis();
  Serial.println("usb: ready");
}

void startWiFi() {
  WiFi.disconnect();
  if (wifiSsid.length() == 0) {
    net = NET_UNCONFIGURED;
    Serial.println("wifi: unconfigured");
    return;
  }
  WiFi.begin(wifiSsid.c_str(), wifiPass.c_str());
  net = NET_CONNECTING;
  netSince = millis();
  Serial.println("wifi: connecting");
}

void startServer();

// 接続の進行を見る(loop から毎回呼ぶ。待ちはしない)
void updateWiFi() {
  if (net == NET_CONNECTING) {
    if (WiFi.status() == WL_CONNECTED) {
      net = NET_CONNECTED;
      if (!serverStarted) startServer();
      Serial.print("ready: http://");
      Serial.println(WiFi.localIP());
      lastRequest = millis();
      shownSince = millis();
    } else if (millis() - netSince > WIFI_TIMEOUT_MS) {
      // 失敗理由を報告して再試行(再起動はしない: シリアル設定を受け付け続けるため)
      Serial.print("wifi: failed status=");
      Serial.println((int)WiFi.status());
      startWiFi();
    }
  }
}

// ---- USB シリアルプロトコル(1 行 1 コマンド、115200bps) ----
// 設定(setup.py が使う):
//   CLAWD?                      → "CLAWD <proto> <usb|unconfigured|connecting|connected <ip>>"
//   WIFI <ssid hex> <pass hex>  → WiFi モードにして NVS に保存・接続(任意の文字を通すため 16 進で受ける)
//   MODE usb                    → USB モードにして NVS に保存(WiFi は切る)
//   FORGET                      → 設定をすべて消去(WiFi 未設定に戻る)
// 状態送信(USB モードで bridge.py が使う。HTTP API と同じ意味):
//   LED <state> <sid> <ts>      → /led 相当。成功時は何も返さない(行を流し続けないため)
//   RGB <r> <g> <b>             → /rgb 相当
//   STATUS                      → GET / と同じ本文のあと "END"
bool hexDecode(const String& hex, String& out) {
  if (hex == "-") { out = ""; return true; }   // 空文字(オープンネットワークのパスワード)
  if (hex.length() % 2) return false;
  out = "";
  for (size_t i = 0; i < hex.length(); i += 2) {
    char buf[3] = {hex[i], hex[i + 1], 0};
    char* end;
    long v = strtol(buf, &end, 16);
    if (*end) return false;
    out += (char)v;
  }
  return true;
}

void handleCommand(String line) {
  line.trim();
  if (line.startsWith("LED ")) {
    // LED <state> <sid> <ts>(sid/ts は省略可)
    String rest = line.substring(4);
    int p1 = rest.indexOf(' ');
    String st = p1 < 0 ? rest : rest.substring(0, p1);
    String sid, ts;
    if (p1 >= 0) {
      int p2 = rest.indexOf(' ', p1 + 1);
      sid = p2 < 0 ? rest.substring(p1 + 1) : rest.substring(p1 + 1, p2);
      if (p2 >= 0) ts = rest.substring(p2 + 1);
    }
    if (!applyState(st, sid, ts)) Serial.println("ERR unknown state");
  } else if (line.startsWith("RGB ")) {
    int r, g, b;
    if (sscanf(line.c_str() + 4, "%d %d %d", &r, &g, &b) == 3) applyRgb(r, g, b);
    else Serial.println("ERR bad rgb command");
  } else if (line == "STATUS") {
    Serial.print(statusBody());
    Serial.println("END");
  } else if (line == "CLAWD?") {
    Serial.print("CLAWD ");
    Serial.print(FW_PROTO);
    if (usbMode) Serial.println(" usb");
    else if (net == NET_CONNECTED) { Serial.print(" connected "); Serial.println(WiFi.localIP()); }
    else Serial.println(net == NET_CONNECTING ? " connecting" : " unconfigured");
  } else if (line.startsWith("WIFI ")) {
    int sp = line.indexOf(' ', 5);
    String ssid, pass;
    if (sp < 0 || !hexDecode(line.substring(5, sp), ssid) || !hexDecode(line.substring(sp + 1), pass) ||
        ssid.length() == 0 || ssid.length() > 32 || pass.length() > 64) {
      Serial.println("ERR bad wifi command");
      return;
    }
    prefs.putString("mode", "wifi");
    prefs.putString("ssid", ssid);
    prefs.putString("pass", pass);
    wifiSsid = ssid;
    wifiPass = pass;
    Serial.println("OK wifi saved");
    if (usbMode) { usbMode = false; wifiInit(); }
    startWiFi();
  } else if (line == "MODE usb") {
    prefs.putString("mode", "usb");
    Serial.println("OK usb mode");
    enterUsbMode();
  } else if (line == "FORGET") {
    prefs.clear();
    wifiSsid = wifiPass = "";
    Serial.println("OK forgotten");
    if (usbMode) { usbMode = false; wifiInit(); }
    startWiFi();
  } else if (line.length() > 0) {
    Serial.println("ERR unknown command");
  }
}

void pollSerial() {
  static String buf;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (buf.length()) handleCommand(buf);
      buf = "";
    } else if (buf.length() < 256) {
      buf += c;
    }
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(BTN_PIN, INPUT);   // GPIO39 は入力専用・基板側プルアップ、押下で LOW
  FastLED.addLeds<WS2812B, LED_PIN, GRB>(leds, 1);
  FastLED.setBrightness(BRIGHTNESS);
  leds[0] = CRGB::Purple; FastLED.show();   // 接続中の目印

  prefs.begin("clawd", false);
  wifiSsid = prefs.getString("ssid", "");
  wifiPass = prefs.getString("pass", "");

  if (prefs.getString("mode", "wifi") == "usb") {
    enterUsbMode();
    return;
  }
  wifiInit();
  startWiFi();
}

void startServer() {
  serverStarted = true;
  MDNS.begin(HOSTNAME);
  MDNS.addService("http", "tcp", 80);

  server.on("/led", handleLed);
  server.on("/rgb", handleRgb);
  server.on("/", []() { server.send(200, "text/plain", statusBody()); });
  server.begin();
}

void loop() {
  pollSerial();
  updateWiFi();
  if (serverStarted) server.handleClient();

  // 前面ボタン: 押下(立ち下がり)でレインボー 10 秒。消灯中でも起きる
  static bool btnPrev = true;
  static unsigned long btnLast = 0;
  bool btn = digitalRead(BTN_PIN);
  if (btnPrev && !btn && millis() - btnLast > 250) {   // 250ms デバウンス
    btnLast = millis();
    rainbowSince = millis();
    rainbowActive = true;
    lastRequest = millis();   // 自動消灯タイマーもリセット(押せば必ず光る)
  }
  btnPrev = btn;

  render();

  // WiFi 断の復旧（数週間置きっぱなしにする前提）
  static unsigned long lastCheck = 0;
  if (millis() - lastCheck > 30000) {
    lastCheck = millis();
    if (net == NET_CONNECTED && WiFi.status() != WL_CONNECTED) ESP.restart();
  }
}

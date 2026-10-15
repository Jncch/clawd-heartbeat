#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <FastLED.h>

// ---- ユーザー設定 ----
#include "secrets.h"   // WIFI_SSID / WIFI_PASS(gitignore 済み)
const char* HOSTNAME  = "atom";
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

CRGB leds[1];
WebServer server(80);

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

void handleLed() {
  String s = server.arg("s");
  State st;
  if      (s == "tool") st = TOOL;
  else if (s == "wait") st = WAIT;
  else if (s == "done") st = DONE;
  else if (s == "err")  st = ERR;
  else if (s == "idle") st = IDLE;
  else { server.send(400, "text/plain", "unknown state\n"); return; }

  String sid = server.arg("sid");
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
  uint64_t ts = strtoull(server.arg("ts").c_str(), nullptr, 10);
  if (ts > 0 && ts < slot->lastTs) {
    slot->seen = now;   // セッションは生きているので失効タイマーだけ更新
    server.send(200, "text/plain", "stale\n");
    return;
  }
  if (ts > 0) slot->lastTs = ts;

  if (slot->st != st) slot->since = now;
  slot->st = st;
  slot->seen = now;

  rawActive = false;
  lastRequest = now;
  server.send(200, "text/plain", "ok\n");
}

// 発光テスト用: /rgb?r=255&g=0&b=0 で任意色を直接点灯(次の /led で通常動作に戻る)
void handleRgb() {
  rawColor = CRGB(server.arg("r").toInt(), server.arg("g").toInt(), server.arg("b").toInt());
  rawActive = true;
  rawSince = millis();
  lastRequest = rawSince;
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
  pinMode(BTN_PIN, INPUT);   // GPIO39 は入力専用・基板側プルアップ、押下で LOW
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
  server.on("/rgb", handleRgb);
  server.on("/", []() {
    const char* names[] = {"idle", "tool", "wait", "done", "err"};
    unsigned long now = millis();
    String body = String("state=") + (rawActive ? "raw" : names[(int)aggregate(now)]) +
      "\nsessions=" + String(activeSessions(now)) +
      "\nuptime=" + String(now / 1000) + "s"
      "\nrssi=" + String(WiFi.RSSI()) + "\n";
    for (auto& s : sessions) {
      if (!s.used || now - s.seen > staleFor(s)) continue;
      body += String("  ") + s.id + " " + names[(int)s.st] +
              " age=" + String((now - s.seen) / 1000) + "s\n";
    }
    server.send(200, "text/plain", body);
  });
  server.begin();

  Serial.print("ready: http://");
  Serial.println(WiFi.localIP());
  Serial.print("mac: ");
  Serial.println(WiFi.macAddress());
  lastRequest = millis();
  shownSince = millis();
}

void loop() {
  server.handleClient();

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
    if (WiFi.status() != WL_CONNECTED) ESP.restart();
  }
}

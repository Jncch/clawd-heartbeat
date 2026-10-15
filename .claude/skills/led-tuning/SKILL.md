---
name: led-tuning
description: >
  Tune LED colors, brightness, and blink/breathing timing to match the user's
  filament and case. Use for requests like "change the color", "too bright /
  too dim", "looks yellow", "can't see the green through the case", "blink
  slower/faster", "make it breathe" — or in Japanese:「色を変えたい」
  「明るすぎる/暗すぎる」「黄色っぽく見える」「緑が見えない」「点滅を遅く/速く」
  「呼吸にして」. Filament pigments change translucency a lot, so iterate:
  measure with led-test.sh → edit one value → flash → check by eye.
---

# LED チューニング手順

原則: **1 回の変更は 1〜2 箇所、毎回書き込んで目視確認**。まとめて変えると
どの変更が効いたかわからなくなる。ユーザーの言語に合わせて進めること。

## 0. まず実測(ケース越しの見え方はフィラメント次第)

ケースに入れた状態で透過テストをしてもらう:

```bash
./led-test.sh coupon    # 実運用の各色を順に点灯(対話式なので通常のターミナルで)
./led-test.sh rgb R G B # 任意色を 10 分間固定(じっくり見る・写真を撮る用)
./led-test.sh ramp R G B # 8 段階の明るさで限界を探る
```

known issues(オレンジ系ケースの場合):
- オレンジ PLA は**緑・青を強く吸収**する。緑がくすむ/青が見えないのは normal。
  壁 0.4mm 超だと悪化する
- **白はピンク〜マゼンタに見える**(緑だけが強く抜けて R+B が残るため)。
  「白いはずがピンク」という報告は故障ではない。ユーザーが白く見せたいなら
  緑を足すのではなく、そのフィラメントで白に見える配合を実測で探す必要がある
- オレンジの光は緑成分を絞らないとレンズ・ケース越しで**黄色に見える**
  (CRGB(255,96,0) でも黄色く見えた実績あり。オレンジにしたいなら G≦40)
- tool と wait は**色相を離す**こと(例: 暖色ケースでオレンジ tool + 赤 wait は混同した実績あり)

## 1. 変更箇所(すべて src/main.cpp)

| 変えたいもの | 場所 |
| :--- | :--- |
| 全体の明るさ上限 | `const uint8_t BRIGHTNESS`(ケース入り: 255、裸: 30〜50) |
| idle の色・明るさ | `case IDLE:` の `CHSV(160, 255, 128)`(最後の値が明るさ) |
| tool の色 | `case TOOL:` の `c = CRGB::White` |
| tool の呼吸速度・振れ幅 | 同 `beatsin8(40, 10, 255)` = (BPM, 最小, 最大)。40BPM ≒ 1.5 秒周期 |
| wait の色・明るさ | `case WAIT:` の `CRGB(170, 0, 0)`(2 箇所) |
| wait の点滅速度 | 同 `(t / 400)` の 400(ms)。err の 120ms に近づけすぎない |
| wait の点滅→常灯切替 | `WAIT_BLINK_MS`(既定 30 秒) |
| done の表示時間 | `DONE_MS`(既定 6 秒)、点滅速度は `(t / 150)` |
| err の点滅速度 | `case ERR:` の `(t / 120)` |

パターンの書き方:
- 常灯: `c = CRGB(...)` / `c = CHSV(...)`
- 点滅: `c = ((t / 周期ms) % 2) ? 色 : CRGB::Black;`
- 呼吸: `c = 色; c.nscale8(beatsin8(BPM, 最小, 最大));`
  (三項演算子で CHSV と CRGB を混在させるとコンパイルエラーになるので注意)

## 2. 書き込みと確認

```bash
pio run -t upload        # 書き込み(自動リセットで即反映)
./led-test.sh states     # 5 状態を順に再生して目視
```

デバイスが応答しなくなったら書き込み直後の再起動中(10 秒ほど待つ)。

## 3. 設計上の注意(変えないほうがいいもの)

- `loop()` に `delay()` を入れない(Web サーバが止まる)
- 状態同士の**見分けやすさ**を保つ: 色だけでなく「常灯/点滅/呼吸」のパターン差も
  識別に効いている。全部呼吸にする等は非推奨
- err(赤高速点滅)と wait(赤点滅)は速度差で区別している。wait を 250ms より
  速くしない
- 色の変更履歴と理由は `docs/NOTES.md` に記録されている。大きく変えるときは追記する

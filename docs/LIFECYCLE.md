# LIFECYCLE — Claude Code のイベントと LED 表示の対応

Claude Code のターンのライフサイクルの各時点で「何の hook が発火し、LED がどうなるか」の整理。
挙動はすべて実測ベース(v2.1.226、検証方法は [NOTES.md](NOTES.md) 参照)。

## 1. 通常のターン(承認なし)

```
あなたがプロンプト送信
  └─ UserPromptSubmit ──────────→ 白の呼吸(tool)
Claude がツールを実行(0〜n 回)
  ├─ PreToolUse ────────────────→ 白の呼吸(tool)
  └─ PostToolUse ───────────────→ 白の呼吸(tool)
Claude が応答を書き終える
  └─ Stop ──────────────────────→ 緑点滅 6 秒(done)→ 青(idle)
```

`PostToolUse` と次の `PreToolUse` の間(Claude の思考中)はイベントが無いが、
LED は直前の tool 表示を維持するので見た目は連続する。

## 2. 権限確認ダイアログ

```
Claude がコマンドを実行しようとする
  ├─ PreToolUse ────────────────→ 白の呼吸(led.sh が呼び出しを記録)
  └─ PermissionRequest ─────────→ 赤点滅(wait)。30 秒で赤常灯に
あなたが Yes を押す
  └─ (イベントなし・無音) ─────→ ⚠ 赤のまま
コマンドが実行される(数秒〜数分)
  └─ (実行中もイベントなし) ───→ ⚠ 赤のまま
コマンドが完了する
  └─ PostToolUse ───────────────→ 白の呼吸に復帰(待っていた呼び出しの完了を照合)
```

**「Yes を押したのに赤いまま」はこの構造のため。** 承認の瞬間を伝えるイベントが
存在せず、次の信号は承認したコマンドの完了(PostToolUse)になる。
つまり Yes 後の赤の長さ = そのコマンドの実行時間。

読み方の目安: **点滅の赤 = まだ答えていない可能性が高い(最初の 30 秒)、
常灯の赤 = 答えた後で長いコマンドが走っている可能性が高い**。

## 3. 選択肢ダイアログ(AskUserQuestion)

```
Claude が選択肢を提示
  └─ PreToolUse(AskUserQuestion)→ 赤点滅(led.sh が wait に変換)
あなたが回答する
  └─ PostToolUse ───────────────→ 白の呼吸に復帰(こちらは回答=完了なので即時)
```

権限確認と違い、回答した瞬間に PostToolUse が発火するので、赤の残留はない。

## 4. 拒否・中断(hook から見えない操作)

以下の操作は**どの hook イベントも発火しない**(実測で確定):

| 操作 | LED の挙動 | 復帰手段 |
| :--- | :--- | :--- |
| ダイアログで No | **トランスクリプト監視が数秒で検知して idle へ**(下記) | 取りこぼし時: 次のプロンプト / 10 分 TTL |
| ダイアログ表示中に Ctrl+C | 同上 | 同上 |
| 作業中に Ctrl+C | 白の呼吸のまま | 次のプロンプト入力 / 10 分で idle |
| 割り込み直後のメッセージ | UserPromptSubmit が発火しないことがある | 次の通常プロンプト |

**トランスクリプト監視**: hook はダイアログへの回答を通知しないが、拒否は
`tool_result`(`The user doesn't want to proceed...`)、中断は
`[Request interrupted by user]` としてセッションのトランスクリプト(JSONL)に
即座に記録される。led.sh はダイアログ表示時に短命の監視プロセスを起動し、
transcript_path の追記分にこれらの構造が現れたら赤を解除する
(JSONL のフィールド構造ごとマッチするため、会話文中の引用では誤発火しない)。
監視はマーカー消失か 10 分で自然終了する。

`PermissionDenied` というイベントは存在するが auto mode classifier 専用で、
手動の No では発火しない(ドキュメント記載どおり)。

## 5. サブエージェント実行中

サブエージェントのツールイベントは**親と同じ session_id** で発火する。
そのまま流すと、メインが承認待ち(赤)でも裏の Agent の PreToolUse/PostToolUse が
白を上書きしてしまうため、led.sh は「ダイアログ応答待ちマーカー」を持つ:

```
PermissionRequest / AskUserQuestion 表示
  → マーカー作成(どの tool_use_id の応答待ちかを記録)
マーカー存在中の tool イベント(サブエージェント等)
  → すべて wait に変換して赤を維持
待っていた呼び出しの PostToolUse / PostToolUseFailure
  → マーカー削除、白に復帰
UserPromptSubmit / done / err / idle / 10 分 TTL
  → マーカー削除(取りこぼし時の保険)
```

補足: PermissionRequest の入力 JSON には tool_use_id が無いため、
直前の PreToolUse で控えた値から紐付けている(詳細は NOTES.md)。

## 6. LED 状態機械とタイマー一覧

| 状態 | 見た目 | 遷移・タイマー |
| :--- | :--- | :--- |
| `idle` | 青の常灯(1/2 輝度) | — |
| `tool` | 白の呼吸(1.5 秒周期、オレンジケース越しではピンクに見える) | 10 分更新なしで idle |
| `wait` | 赤点滅 400ms | 30 秒(WAIT_BLINK_MS)で赤常灯へ |
| `wait`(常灯) | 赤の常灯 | 開始から 10 分(STALE_MS)で idle |
| `done` | 緑点滅 150ms | 6 秒(DONE_MS)で idle(他セッション作業中ならそちらへ) |
| `err` | 赤の高速点滅 120ms | 次のイベントか 10 分で idle |
| 消灯 | — | 最終リクエストから 30 分(OFF_MS) |

複数セッションは `wait > err > done > tool > idle` の優先度で集約。
手動送信(sid=default)だけは 2 分(DEFAULT_STALE_MS)で失効する。

## 7. おかしいと思ったら

```bash
./led-test.sh status
```

集約状態とセッション別の内訳(どのセッションが何の状態か、最終更新からの経過秒)が
出るので、「どこが赤を握っているか」が一目でわかる。

- default が残っている → 手動テストの残留。`./led-test.sh off` か 2 分待つ
- 実セッションが wait のまま → ダイアログ放置か、拒否/中断の残留(セクション 4)
- 何も点いていない → 30 分無通信の自動消灯。何かイベントを送れば復帰

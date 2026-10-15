#!/usr/bin/env bash
# アニメーション自動再生(撮影・動作確認用)
#
# 対話操作なしで各状態を一定時間ずつ再生する。録画を開始してから放置できる。
#
#   ./led-demo.sh                 # 全状態を順に再生(既定)
#   ./led-demo.sh clip            # 動画/GIF 用の 16 秒シーケンス(撮影はこれが楽)
#   ./led-demo.sh story           # 実運用の流れを再現(作業→承認待ち→承認後→完了)
#   ./led-demo.sh wait-full       # wait の点滅→常灯の切替(30秒)まで見せる
#   ./led-demo.sh states --loop   # 中断するまで繰り返す
#   ./led-demo.sh states --lead 10  # 開始前に 10 秒の準備時間を入れる(既定 5 秒)
#   ./led-demo.sh states --solo   # 他セッションを idle にして確実に再生(撮影向け)
#
# 注意: 他の Claude Code セッションが作業中だと優先度集約で上書きされる。
# --solo を付けるか、通常のターミナルから実行すること(実セッションの表示は
# 次の hook イベントで自然に復帰する)。
set -eu

# 宛先の決定: 環境変数 ATOM > .atom-ip ファイル(gitignore 済み) > プレースホルダ
here=$(cd "$(dirname "$0")" && pwd)
if [ -z "${ATOM:-}" ] && [ -f "$here/.atom-ip" ]; then
  ATOM=$(cat "$here/.atom-ip")
fi
ATOM="${ATOM:-http://atom.local}"

mode="states"
loop=0
lead=5
solo=0
while [ $# -gt 0 ]; do
  case "$1" in
    states|story|wait-full|clip) mode="$1"; shift ;;
    --loop) loop=1; shift ;;
    --solo) solo=1; shift ;;
    --lead) lead="${2:?seconds}"; shift 2 ;;
    -h|--help) grep '^#   ' "$0" | sed 's/^#   //'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

send() { curl -4 -s -m 2 "$ATOM/led?s=$1&sid=demo" >/dev/null; }

# 他セッションが動いていると優先度集約で上書きされるので警告する
check_conflict() {
  local others
  others=$(curl -4 -s -m 2 "$ATOM/" 2>/dev/null | awk 'NR>4 && $2 != "idle" && $1 != "demo" {print "  " $0}') || return 0
  if [ -n "$others" ]; then
    echo "⚠ 他のセッションが待機中以外の状態です。再生が上書きされる可能性があります:"
    echo "$others"
    echo "  (Claude Code を終了するか、そのターンが終わるのを待つと確実です)"
    echo
  fi
}

# 他セッションを idle に落として再生を独占する(--solo)。
# 実セッションの表示は次の hook イベントで自然に復帰するので副作用は一時的。
silence_others() {
  local ids id
  ids=$(curl -4 -s -m 2 "$ATOM/" 2>/dev/null | awk 'NR>4 && $1 != "demo" {print $1}') || return 0
  for id in $ids; do
    curl -4 -s -m 2 "$ATOM/led?s=idle&sid=$id" >/dev/null 2>&1 || true
  done
  [ -n "$ids" ] && echo "他セッションを idle にしました: $(echo "$ids" | tr '\n' ' ')"
  return 0
}

# hold <state> <seconds> <説明>
hold() {
  local state="$1" secs="$2" label="$3" i
  send "$state"
  for i in $(seq "$secs" -1 1); do
    printf '\r  %-6s %-34s %2ds ' "$state" "$label" "$i"
    sleep 1
  done
  printf '\r  %-6s %-34s done\n' "$state" "$label"
}

play_states() {
  hold idle 5 "青の常灯(待機)"
  hold tool 9 "白の呼吸(作業中)"
  hold wait 6 "赤の点滅(承認待ち)"
  hold done 7 "緑の点滅(完了)→ 待機へ"
  hold err  5 "赤の高速点滅(エラー)"
  hold idle 4 "青の常灯(待機)"
}

play_story() {
  hold idle 4 "待機中"
  hold tool 8 "プロンプト送信 → 作業中"
  hold wait 8 "権限確認ダイアログ(呼ばれている)"
  hold tool 6 "承認 → コマンド実行中"
  hold done 7 "ターン完了"
  hold idle 4 "待機に戻る"
}

# 動画・GIF 用に詰めた 16 秒。SNS / README で最後まで見てもらえる長さに収めている
play_clip() {
  hold idle 2 "待機(青)"
  hold tool 4 "作業中(呼吸)"
  hold wait 5 "承認待ち(赤の点滅)← 主役"
  hold tool 2 "承認後、作業再開"
  hold done 3 "完了(緑)"
}

play_wait_full() {
  hold idle 3 "待機中"
  hold wait 40 "点滅 30 秒 → 常灯に切り替わる"
  hold idle 4 "待機に戻る"
}

cleanup() {
  printf '\r%-60s\r' ' '
  send idle 2>/dev/null || true
  echo "再生を終了しました(demo セッションは 10 分で自動失効します)"
}
trap cleanup EXIT INT TERM

if [ "$solo" -eq 1 ]; then
  silence_others
else
  check_conflict
fi
echo "再生モード: $mode  宛先: $ATOM"
if [ "$lead" -gt 0 ]; then
  for i in $(seq "$lead" -1 1); do
    printf '\r  録画の準備をしてください… %2ds ' "$i"
    sleep 1
  done
  printf '\r%-40s\r' ' '
fi

while :; do
  case "$mode" in
    states)    play_states ;;
    clip)      play_clip ;;
    story)     play_story ;;
    wait-full) play_wait_full ;;
  esac
  [ "$loop" -eq 1 ] || break
  echo "--- 繰り返し(Ctrl-C で終了)---"
done

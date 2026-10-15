#!/usr/bin/env bash
# 発光テスト用スクリプト
#
# 使い方:
#   ./led-test.sh coupon          # テストピース透過テスト(Enter で次の色へ)
#   ./led-test.sh states          # 実際の 5 状態を順に再生(Enter で次へ)
#   ./led-test.sh rgb R G B       # 任意色を直接点灯 (例: ./led-test.sh rgb 0 255 0)
#   ./led-test.sh ramp R G B      # 指定色を暗→明に 8 段階でランプ(Enter で次へ)
#   ./led-test.sh off             # 消灯(idle に戻す)
set -eu

# 宛先の決定: 環境変数 ATOM > .atom-ip ファイル(gitignore 済み) > プレースホルダ
here=$(cd "$(dirname "$0")" && pwd)
if [ -z "${ATOM:-}" ] && [ -f "$here/.atom-ip" ]; then
  ATOM=$(cat "$here/.atom-ip")
fi
ATOM="${ATOM:-http://atom.local}"

rgb() { curl -4 -s -m 2 "$ATOM/rgb?r=$1&g=$2&b=$3" >/dev/null; }
led() { curl -4 -s -m 2 "$ATOM/led?s=$1" >/dev/null; }

case "${1:-}" in
  coupon)
    # ケース素材の透過確認用。フル輝度の原色 → 実運用で厳しい色、の順
    echo "テストピースを LED にかざして、各色の見え方を確認してください"
    while :; do
      for entry in "紫(tool 相当・ピーク時):80:0:170" "赤(wait 相当・ピーク時):170:0:0" "緑(done 相当):0:255:0" "赤(フル):255:0:0" "青(フル):0:0:255" "白(フル):255:255:255" "青(idle 相当):0:0:170"; do
        IFS=: read -r name r g b <<< "$entry"
        rgb "$r" "$g" "$b"
        read -rp "→ $name を点灯中。Enter で次へ(Ctrl-C で終了) "
      done
      echo "--- 一巡しました。もう一周します ---"
    done
    ;;
  states)
    for s in idle tool wait done err; do
      led "$s"
      read -rp "→ $s を再生中。Enter で次へ "
    done
    led idle
    ;;
  rgb)
    rgb "${2:?r}" "${3:?g}" "${4:?b}"
    echo "rgb($2, $3, $4) 点灯中(/led を叩くか 10 分で通常動作に戻ります)"
    ;;
  ramp)
    r="${2:?r}"; g="${3:?g}"; b="${4:?b}"
    for pct in 2 5 10 20 40 60 80 100; do
      rgb $((r * pct / 100)) $((g * pct / 100)) $((b * pct / 100))
      read -rp "→ ${pct}% を点灯中。Enter で次へ "
    done
    ;;
  off)
    led idle
    echo "idle に戻しました"
    ;;
  status)
    curl -4 -s -m 2 "$ATOM/"
    ;;
  send)
    # 順序保証(ts)等のデバッグ用: ./led-test.sh send <state> <sid> <ts>
    curl -4 -s -m 2 "$ATOM/led?s=${2:?state}&sid=${3:-default}&ts=${4:-0}"
    ;;
  *)
    grep '^#   ' "$0" | sed 's/^#   //'
    exit 1
    ;;
esac

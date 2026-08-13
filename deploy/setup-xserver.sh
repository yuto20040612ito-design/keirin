#!/bin/sh
# 共用レンタルサーバー(エックスサーバー等)に収集環境を用意する。
#
#   sh deploy/setup-xserver.sh
#
# やること:
#   1. Python のバージョンを確認する
#   2. 収集を1回だけ試して、動くことを確かめる
#   3. cron に貼るコマンドをそのまま出す
#
# 何も壊さない。設置と確認だけで、cron 登録は人がやる(サーバーパネルからのため)。
# sh で書いてあるのは、共用サーバーに bash が無い場合があるため。

set -e

echo ""
echo "=================================================="
echo " 競輪データ収集 セットアップ確認"
echo "=================================================="

# --- 1. 場所の確認 ---------------------------------------------------------

if [ ! -f "src/keirin/collect.py" ]; then
    echo ""
    echo "  [NG] リポジトリのルートで実行してください。"
    echo "       cd ~/keirin && sh deploy/setup-xserver.sh"
    exit 1
fi
ROOT=$(pwd)
echo ""
echo "  設置場所: $ROOT"

# --- 2. Python の確認 ------------------------------------------------------

PY=""
for c in python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
    if command -v "$c" >/dev/null 2>&1; then
        # 3.8 以上かどうかを本人に判定させる
        if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
            PY=$(command -v "$c")
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo ""
    echo "  [NG] Python 3.8 以上が見つかりませんでした。"
    echo ""
    echo "       入っている Python:"
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1; then
            echo "         $c -> $("$c" --version 2>&1)"
        fi
    done
    echo ""
    echo "       この出力をそのまま貼って相談してください。"
    exit 1
fi

echo "  Python  : $PY ($("$PY" --version 2>&1))"

# --- 3. 実際に1回収集してみる ----------------------------------------------

echo ""
echo "  収集を1回試します (30秒ほどかかります)..."
echo "  --------------------------------------------------"
if PYTHONPATH=src "$PY" -m keirin.collect once; then
    echo "  --------------------------------------------------"
    echo "  [OK] 収集は動きました。"
else
    echo "  --------------------------------------------------"
    echo "  [NG] 収集に失敗しました。上のエラーを貼って相談してください。"
    exit 1
fi

# --- 4. cron に貼る文字列を出す --------------------------------------------

echo ""
echo "=================================================="
echo " 次にやること: サーバーパネルの Cron 設定"
echo "=================================================="
echo ""
echo "  分・時・日・月・曜日 は すべて  *  (毎分)"
echo ""
echo "  コマンド欄に以下をそのまま貼り付け:"
echo ""
echo "cd $ROOT && PYTHONPATH=src $PY -m keirin.collect once >> \$HOME/keirin-cron.log 2>&1"
echo ""
echo "  ログが育ちすぎないよう、月1回消す cron も足しておくとよい:"
echo "    0 4 1 * *  :> \$HOME/keirin-cron.log"
echo ""
echo "=================================================="
echo " 登録したあとの確認"
echo "=================================================="
echo ""
echo "  10分ほど待ってから、これを実行:"
echo ""
echo "    cd $ROOT && PYTHONPATH=src $PY -m keirin.collect status"
echo ""
echo "  「収集は動いているように見える」と出れば完了。"
echo "  収集は静かに止まるので、週に一度は見ること。"
echo ""

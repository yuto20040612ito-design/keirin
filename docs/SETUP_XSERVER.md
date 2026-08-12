# エックスサーバー（共用レンタルサーバー）での収集手順

共用サーバーは**常駐プロセスが禁止・強制終了される**ので、`watch` は使えない。
代わりに cron から `once` を毎分呼ぶ。1回ぶんの仕事をして即座に終了するので、
共用サーバーの制約に収まる。

VPS や自宅サーバーなら `watch` の常駐のほうが素直なので、README のほうを見ること。

---

## 1. SSH で入る

サーバーパネル → 「SSH設定」でSSHを**ON**にし、公開鍵を登録する。

```bash
ssh -p 10022 あなたのサーバーID@あなたのサーバーID.xsrv.jp
```

ポートが 10022 なのがエックスサーバーの特徴。22 ではつながらない。

## 2. Python のバージョンを確認する

```bash
python3 --version
```

**3.8 以上なら そのまま進める。** 収集部分は標準ライブラリだけで動くので、
pip でのインストールは何も要らない。

3.7 以下だった場合は、ホームディレクトリに新しい Python を入れる必要がある。
その場合は出力を貼ってくれれば手順を出す。

## 3. 設置する

```bash
cd ~
git clone https://github.com/yuto20040612ito-design/keirin.git
cd keirin
git checkout claude/keirin-victory-database-oix02a
```

動作確認:

```bash
cd ~/keirin
PYTHONPATH=src python3 -m keirin.collect once
```

こう出れば成功:

```
20260813: 67 races / 7 venues (青森9R, 弥彦12R, ...)
once: 67 races known, 0 odds snapshots saved
```

`0 odds snapshots` は正常。締切が近いレースが無ければ何も取らない。

## 4. cron に登録する

サーバーパネル → 「Cron設定」→ 「追加」。

| 項目 | 値 |
|---|---|
| 分 | `*` |
| 時 | `*` |
| 日 | `*` |
| 月 | `*` |
| 曜日 | `*` |

コマンド:

```
cd $HOME/keirin && PYTHONPATH=src /usr/bin/python3 -m keirin.collect once >> $HOME/keirin-cron.log 2>&1
```

`python3` の場所が違う場合は `which python3` で確認して置き換える。

これで毎分起動し、**締切が近いレースだけ**オッズを取る。
何も取るものが無い分は数秒で終わるので、サーバーへの負荷はほとんど無い。

### メール通知を止める

cron の実行結果が毎分メールで飛んでくると鬱陶しい。
上のコマンドのようにログファイルへ流しておけば飛ばない。

ログが無限に育つのを防ぐなら、月1回くらいで消す cron を足しておく:

```
0 4 1 * * : > $HOME/keirin-cron.log
```

## 5. 止まっていないか確認する

**これが運用でいちばん大事。** 収集は静かに止まる。
落ちた日のオッズは二度と手に入らない。

```bash
cd ~/keirin && PYTHONPATH=src python3 -m keirin.collect status
```

```
  最後に取得できた時刻:
    オッズ          2026-08-13 12:31  (0.1時間前)
    ...
  → 収集は動いているように見える。
```

24時間以上オッズが取れていなければ警告が出る。週に一度でいいので見ること。

---

## cron 方式の弱点

`watch` と違い、**cron が止まればその間の予定は丸ごと飛ぶ**。
`watch` は自分で次の予定まで眠るが、cron は外から起こされないと何もしない。

- サーバーメンテナンスで cron が止まる → その時間帯のオッズが欠ける
- 実行が詰まって遅れる → 予定時刻を跨いだぶんはまとめて1回だけ取る

欠けても致命傷ではない（レース結果と確定オッズは後から取れる）が、
**締切前オッズの推移だけは戻らない**。だから status での確認を習慣にすること。

## データはどこに貯まるか

```
~/keirin/data/raw/     生データ。これが本体
~/keirin/data/state/   cron 用の状態ファイル
```

`data/` は Git 管理から外してある（`.gitignore`）。
**サーバーが飛べば消えるので、ときどき手元に落としておくこと。**

```bash
# 手元のPCから
rsync -avz -e 'ssh -p 10022' \
  あなたのサーバーID@あなたのサーバーID.xsrv.jp:keirin/data/raw/ ./data/raw/
```

## 分析はサーバーでやらない

`keirin.load` 以降は DuckDB や numpy が要る。共用サーバーには入れにくいし、
入れる必要もない。**サーバーは収集だけ、分析は手元のPC**で分ければよい。

上の rsync で raw を落として、手元で:

```bash
python -m keirin.load --data-root data --db data/keirin.duckdb
python -m keirin.baseline --db data/keirin.duckdb
```

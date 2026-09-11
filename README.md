# Chrome History Exporter

Chrome の閲覧履歴を一定間隔でファイルに書き出す Windows デスクトップ向けツールです。
バックグラウンド常駐、またはタスクスケジューラによる定期実行に対応しています。

- 出力ファイル名は期間を表す **`YYYYMMDDhhmm-YYYYMMDDhhmm`** 形式
  （例: `202609111200-202609111300.csv` = 12:00〜13:00 の履歴）
- 前回の期間の終端から続けて書き出すので、取りこぼしも重複もありません
- Chrome 起動中でも読み取れます（History を一時コピーしてから参照）
- 追加ライブラリ不要（Python 標準ライブラリのみ）

## 必要なもの

- Windows 10 / 11
- Python 3.10 以降（[python.org](https://www.python.org/downloads/windows/) のインストーラで「Add python.exe to PATH」にチェック）

## セットアップ

```bat
cd chrome_exporter
python run.py init-config
```

`%LOCALAPPDATA%\ChromeHistoryExporter\config.json` が作られます。出力先や間隔を変えたい場合はこれを編集してください。

動作確認:

```bat
python run.py profiles
python run.py once
```

`once` を実行すると出力先（既定 `%USERPROFILE%\Documents\ChromeHistory`）に
`202609111200-202609111300.csv` のようなファイルが作られます。

## バックグラウンドで動かす

### 方法 1: タスクスケジューラに登録（推奨）

```bat
python run.py install
```

ログオン時に `pythonw.exe`（コンソールを出さない Python）で常駐プロセスが起動し、
`interval_minutes` 間隔でエクスポートします。すぐ開始するには:

```bat
schtasks /run /tn ChromeHistoryExporter
```

常駐ではなく「スケジューラが N 分ごとに 1 回だけ実行する」方式にもできます。
PC が起動していない時間帯を挟んでも次回実行時にまとめて書き出されます。

```bat
python run.py install --mode interval
```

解除:

```bat
python run.py uninstall
```

### 方法 2: スタートアップ フォルダ

`scripts\start-background.vbs` のショートカットを `shell:startup`
（エクスプローラのアドレスバーに入力すると開きます）に置くと、
ログオン時にコンソールなしで常駐起動します。

### 常駐の停止

```bat
python run.py stop
```

`scripts\stop-background.bat` でも同じことができます。多重起動はロックファイルで防止されます。

## コマンド一覧

| コマンド | 説明 |
| --- | --- |
| `python run.py once` | 前回の続きから現在までを 1 ファイルに書き出す |
| `python run.py run` | 常駐して `interval_minutes` ごとに書き出す |
| `python run.py stop` | 常駐プロセスに停止を要求する |
| `python run.py status` | 設定・前回実行・次回対象期間を表示する |
| `python run.py profiles` | 検出された Chrome プロファイルを一覧表示する |
| `python run.py init-config` | 設定ファイルのひな形を作る |
| `python run.py install` / `uninstall` | タスクスケジューラへの登録 / 解除 |

共通オプション: `--config <path>`（設定ファイルを指定）、`--log-level DEBUG`。
`scripts\*.bat` はこれらをダブルクリックで実行するためのラッパーです。

## 設定（config.json）

| キー | 既定値 | 説明 |
| --- | --- | --- |
| `browser` | `"chrome"` | `chrome` / `edge` / `brave`。`user_data_dir` 未指定時の自動検出に使う |
| `user_data_dir` | `null` | ユーザーデータフォルダ。`null` なら自動検出 |
| `profiles` | `["*"]` | 対象プロファイル名。`["*"]` で全部（`Default`, `Profile 1` …） |
| `output_dir` | `null` | 出力先。`null` なら `%USERPROFILE%\Documents\ChromeHistory` |
| `output_format` | `"csv"` | `csv` / `jsonl` / `json` |
| `encoding` | `"utf-8-sig"` | CSV を Excel で開くため既定は BOM 付き UTF-8 |
| `interval_minutes` | `60` | 常駐モードのエクスポート間隔 |
| `initial_lookback_hours` | `24` | 初回実行時にさかのぼる時間 |
| `skip_empty` | `true` | 履歴が 0 件の期間はファイルを作らない |
| `state_file` / `log_file` | `null` | `%LOCALAPPDATA%\ChromeHistoryExporter\` 配下が既定 |
| `log_level` | `"INFO"` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `log_max_bytes` / `log_backup_count` | `1000000` / `3` | ログのローテーション設定 |

## 出力内容

| 列 | 内容 |
| --- | --- |
| `visit_time` | 訪問時刻（ISO 8601・ローカルタイム） |
| `profile` | Chrome のプロファイル名 |
| `title` | ページタイトル |
| `url` | URL |
| `transition` | 遷移種別（`link`, `typed`, `reload`, `form_submit` など） |
| `visit_duration_sec` | 滞在秒数（Chrome が記録している場合） |
| `visit_count` | その URL の累計訪問回数 |
| `visit_id` | Chrome 内部の訪問 ID |

## 仕組みと注意点

- 対象期間は `[前回の終端, 今回の実行時刻)` です。実行時刻は分単位に切り捨てられ、
  終端は次回の開始になるため、履歴が重複したり抜けたりしません。
- 期間の管理は状態ファイル（`state.json`）が担います。削除すると次回は
  `initial_lookback_hours` 分さかのぼって再取得します。
- 同じ期間のファイルが既にある場合は `...-1.csv`, `...-2.csv` と連番が付きます。
- ファイルは一時ファイルに書いてから最終名へ移動するので、書きかけのファイルが
  他のツールから見えることはありません。
- Chrome 側で履歴を削除した場合、削除前にエクスポート済みの分はファイルに残ります。
- 出力ファイルには閲覧履歴がそのまま含まれます。共有フォルダやクラウド同期フォルダを
  出力先にする場合は取り扱いに注意してください。

## テスト

```bat
python -m unittest discover -s tests
```

Chrome の History と同じ構造のテスト用データベースを作って、ファイル名の形式・期間の
継続・ロック中の読み取り・出力形式などを検証します。

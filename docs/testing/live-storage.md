# Azure / R2 実環境統合テスト

通常の `uv run pytest` はクラウドへ接続しません。実環境テストは、専用環境の引き継ぎ情報を確認した上で `tests/live_storage.py --run` を明示的に起動します。ストレージ処理・SDKを変更したときやリリース前の検証に使います。

インフラ、RBAC、公開設定、保持期間の作成・変更は基盤リポジトリの担当です。このテストは既存の専用コンテナ／バケットへ小さな生成画像だけを送信します。実際のAPI利用・転送には料金が発生する場合があります。

## 確認する内容

- CLIの画像取り込みとPNGからWebPへの変換。
- push/pullのdry-runで、ローカル保存領域とリモートオブジェクトを変更しないこと。
- 条件付きアップロード、一覧、SHA-256、Content-Type、Cache-Control、独自メタデータ。
- 既存の生成画像に対する匿名GETの拒否。404や通信失敗を非公開確認の成功としません。
- 再push/pullの変更なし判定、`status --remote`、`verify --remote`。
- 独立した受信側data/state/notesへのダウンロードとバイト一致。
- リモート変更との競合拒否、pullによる更新、ユーザー編集済みFrontmatterと本文の保持。
- 重複作成・古いETagによるPUT/GETの拒否、拒否後のオブジェクト不変。
- R2での公開状態不明の確認フローと、今回のmanifestだけを対象にした後片付け。

CLIはこのcheckoutの `cli.main` をプロセス内で呼び、parser・設定読み込み・同期・画像変換・実SDKを通します。接続先をユーザー設定から読み込んだ後、試験対象CLIの設定探索先は空の試験用フォルダへ差し替え、実行専用の設定を渡します。通常の source 設定は試験に使いません。インストール済みconsole-script launcherの実接続試験ではありません。

## 接続先の登録

`uv sync --locked` で開発環境を準備します。リポジトリ移動後は、現在の `.venv/Scripts/python.exe` とR2起動補助のパスを確認してください。テストプログラムは現在のcheckoutの `src` を優先して読みます。

普段の `~/.tkn/object_storage_catalog/config.yaml` に、非秘密の接続先を `integration_tests` として追加します。通常の `sources` はそのまま残します。先に `uv tool install . --reinstall` でインストール済みCLIを0.4.0以降へ更新し、設定の `schema_version` を `"3.1.0"` にしてください。

以下は追加する部分の例です。既存の設定全体を置き換えず、承認済みの引き継ぎ資料の値へ書き換えてください。ハッシュ欄も、後述のレビュー時に算出した64桁の値が必要です。

```yaml
schema_version: "3.1.0"
# 既存の sources は保持する
integration_tests:
  azure:
    provider: azure
    endpoint_url: https://examplestorage.blob.core.windows.net
    bucket: example-tests
    cleanup: retain
    expected_target_sha256: "<reviewed-azure-target-sha256>"
  r2:
    provider: r2
    endpoint_url: https://00000000000000000000000000000000.r2.cloudflarestorage.com
    bucket: example-tests
    cleanup: manifest
    expected_target_sha256: "<reviewed-r2-target-sha256>"
```

Azureでは `bucket` がコンテナ名を表します。`cleanup` は必ず `retain` で、削除APIは呼びません。本人が事前にAzure CLIへログインした状態で実行します。Managed Identityはこのランナーでは試験しません。

R2は対象バケット限定キーを登録済みの基盤側DPAPI起動補助を使います。実行用の設定では `s3.profile: null` を固定し、子プロセスのAWS環境変数を利用します。`.env`、YAML、共有credentials、コマンド引数へキーを複製しないでください。

接続先は、引き継ぎ資料とendpoint・コンテナ／バケット・削除方針を照合してから、以下で正規化SHA-256を計算します。表示された値をそれぞれの `expected_target_sha256` へ記録します。照合用ハッシュは秘密値ではありません。

```powershell
@'
import sys
from pathlib import Path
sys.path.insert(0, "tests")
from live_storage import configuration, target_digest
path = Path.home() / ".tkn/object_storage_catalog/config.yaml"
config = configuration.parse_yaml(path.read_text(encoding="utf-8-sig"), "test config")
for name, settings in config["integration_tests"].items():
    target = {key: value for key, value in settings.items() if key != "expected_target_sha256"}
    print(name, target_digest(target))
'@ | & .venv/Scripts/python.exe -
```

接続先を書き換えてその場で毎回ハッシュを計算し直すと、誤接続防止の照合になりません。接続先を変える場合は再レビューします。ハッシュは暗号的な権限制限の代わりではありません。

ランナーは未知の設定項目、秘密値入りURL、サービス以外のendpoint、`test` または `tests` の区切り語がない対象名、Azureの削除指定を拒否します。対象名の条件だけでテスト環境を保証するものではないため、引き継ぎ資料との照合が必要です。

## 設定の検査と実行

まず、クラウドへの接続・認証・画像生成・ファイル書き込みをせずに、設定とハッシュの一致を検査できます。

```powershell
& .venv/Scripts/python.exe tests/live_storage.py --check --test-target azure
& .venv/Scripts/python.exe tests/live_storage.py --check --test-target r2
```

既定ではユーザー設定だけを読みます。別ファイルは `--config 'C:/path/to/config.yaml'` で指定します。通常CLIと異なり、プロジェクト設定・旧ユーザー設定・他の設定ファイルとの合成は行いません。指定した登録名がなければエラーになります。

Azureを実行する場合は、PowerShellから以下を実行します。毎回新しいUTC日時＋UUIDのrun IDが生成され、再利用できません。

```powershell
& .venv/Scripts/python.exe tests/live_storage.py --run --test-target azure
```

R2はPowerShell 7で、移動後の実際のパスを指定します。

```powershell
$platformRoot = 'D:/path/to/tkn-cloudflare-platform'
$catalogRoot = (Get-Location).Path
& "$platformRoot/scripts/Invoke-R2TestProcess.ps1" `
  -Executable "$catalogRoot/.venv/Scripts/python.exe" `
  -Arguments @(
    "$catalogRoot/tests/live_storage.py", '--run', '--test-target', 'r2'
  )
```

DPAPI登録済みの本人と同じWindowsユーザーで実行します。起動補助が失敗した場合は、パス・ユーザー・登録状況を確認し、キーを表示して診断しないでください。SDKのデバッグログや環境変数一覧も出力しません。

0.3.1で使っていた `.local/integration-targets/*.json` は読み込まなくなりました。必要な値を `integration_tests` へ移した後も、旧ファイルは自動削除しません。旧引数 `--target` / `--expected-target-sha256` は使えません。

## 保存先と後片付け

リモートは `runs/<run-id>/` に分離します。prefixはアクセス権の境界ではありません。run開始時に同じprefixが空であることを確認します。

Windowsのパス長制限を避けるため、ローカルの画像・設定・data/state/notesはOSの一時領域に作る短い `osc-live-*` フォルダへ分離し、検証証跡として残します。永続的な結果は `.local/integration/<run-id>/` に保存します。

| ファイル | 内容 |
| --- | --- |
| `report.json` | チェックごとの結果、失敗時の例外型とHTTP status、残存数、未検証事項 |
| `manifest.json` | 今回生成したキー、asset ID、許可する生成内容のSHA-256 |
| `workspace.json` | 今回の短いローカル作業先。個人パスを含むため共有・commitしない |

終了コードは成功0、失敗1です。チェック失敗だけでなくR2の後片付け失敗も失敗として報告します。失敗したrunの結果は後続runで上書きしません。開始前の引数・接続先検証失敗は標準出力に秘密を含まないエラーだけを返します。

- **R2:** 書き込み前にmanifestへ登録した今回の生成キーだけを、finallyで削除します。接続先・run ID・固定の生成ファイル名・asset ID・実データのハッシュを照合します。不一致のオブジェクトは削除しません。一覧結果を削除対象には使わず、削除後の同じprefixの残存確認だけに使います。
- **Azure:** 成否にかかわらず削除しません。今回のprefixのactiveオブジェクト数を記録し、基盤で設定済みのライフサイクルへ任せます。soft delete/snapshotを含む完全な保持量はこの件数に含みません。
- ローカル試験フォルダを片付ける場合は、`workspace.json` が指す今回の生成領域だけを確認して削除してください。このランナーに過去runの一括削除機能はありません。

プロセス強制終了・ネットワーク障害などではR2のfinallyが完了しないことがあります。manifestを保持し、基盤の保持設定を補完として使います。本番や他runを含む一括削除は行いません。

## 検証範囲の限界

AWS S3、Managed Identity、実行をまたぐ並列競合、負荷・大容量、ライフサイクルの経時削除・復元、課金額、CDN/独自ドメイン、Worker binding、インフラ構成全体は対象外です。通常のunit testも引き続き併用します。

生成画像の匿名拒否はその実行時点の結果です。本番バケットへの接続やキーの権限監査はこのランナーでは行いません。基盤の受入検証結果と区別してください。

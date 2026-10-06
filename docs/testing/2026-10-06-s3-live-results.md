# 2026-10-06 AWS S3 実環境統合テスト結果

CLI 0.5.0のS3実環境テストで **32/32項目が成功** しました。manifestに登録した生成画像2件だけをETag条件付きで削除し、今回のrun prefixの残存0件を確認しました。基盤側のSDK受入に加え、CLIのpush/pull/status/verify・dry-run・ノート保持を検証した結果です。

## 実行記録

| 項目 | 結果 |
| --- | --- |
| 実行日時 | 2026-10-06 14:37:44–14:38:01 JST |
| run ID | `20261006T053744Z-99be5350920b431bb25fa25af04e0831` |
| provider / region | AWS S3 / ap-northeast-1 |
| 使用設定 | 現行ユーザーconfig.yamlの `integration_tests.s3`。スキーマ3.2.0 |
| 認証 | 専用AWS profile → credential_process / AssumeRole。STSのaccount/role照合成功 |
| 対話ログイン | 追加のログイン・MFAなしで既存セッションを利用 |
| リモート範囲 | `runs/<上記run-id>/` の生成画像だけ |
| ローカル範囲 | 今回専用の短い一時フォルダー内でsender/receiverのdata/state/notesを分離 |
| 最終結果 | 32項目成功、後片付け成功、終了コード0 |
| 削除 | manifestのkey・asset ID・実SHA-256を照合し、確認したETagでIfMatch DELETE。2件削除、残存0 |

実account、bucket、role ARN、個人パスはこの共有記録へ含めません。詳細はローカルの `.local/integration/<run-id>/report.json`、`manifest.json`、`workspace.json` に保存しています。

## 確認した動作

- ストレージ接続前のSTS照合、新しいrun prefixの空確認。
- 未存在キーのHEADが404であることと、adapterがそのキーをNoneとして扱うこと。403を未存在扱いにする変更は行っていません。
- PNG取り込みとWebP変換、push/pullのdry-runによる非変更。
- 公開状態不明のpushに確認が必要で、確認前にはリモート画像が作られないこと。
- 実転送、LIST、SHA-256、Content-Type、Cache-Control、独自メタデータの一致。
- 存在を認証付きで確認した生成画像への匿名GETが **403 AccessDenied** となること。404、401、R2固有の400応答は成功条件に含めません。
- 再push/pullの変更なし判定、remote status/verify、独立receiverへのダウンロード。
- リモート更新との競合拒否と再取得、利用者が編集したFrontmatter・本文・IDの保持。
- 条件付きPNG作成・更新、重複作成の拒否、古いETagによるPUT/GETの拒否、拒否後の内容保持。
- manifestだけを対象とするIfMatch DELETEと、今回のprefixの残存0確認。後片付けの成否は32チェックとは別に記録しています。

## 実装・設定とローカル検証

既存のS3実装を確認し、STSクライアントをcontext managerとして直接使用していた箇所を、実SDKのcloseに対応する処理へ修正しました。最初の事前確認はTypeErrorで停止し、S3への接続・画像生成は行っていません。mockがこの不整合を隠していたため、実SDKクライアントとStubberによる回帰テストも追加しました。

設定は現行アプリ保存先 `~/.tkn/objstorage-imgcatalog/config.yaml` に集約しました。引き継ぎ資料の旧 `object_storage_catalog` パスは移行済みのため使っていません。元ファイルのバックアップを作成し、通常のsources 2件とAzure/R2の既存テスト設定が変更されていないことを比較検証しています。秘密値は保存していません。

- 通常テスト: **286 passed**。クラウド接続なし。
- `ruff check .`、`mypy src`: 成功。
- 0.5.0のsdist / wheel作成: 成功。
- インストール済み `tkn-objstorage-imgcatalog`: 0.5.0へ更新、version / config list確認成功。
- Azure / R2 / S3すべての `--check`: 成功。認証・クラウド接続なし。
- 未存在HEADの404と403例外伝播、S3の誤account/role拒否、manifest外・内容不一致・競合時の削除拒否、cleanupだけが失敗した場合の終了コード1もオフラインで検証。

## 未検証事項と変更範囲

1時間を超える認証更新、期限切れ時の再ログイン、7日経過後のライフサイクル削除、負荷・大容量・multipart・実行間の並列競合、課金実績、CDN/独自ドメイン経由の配信は未検証です。インストール済みconsole-script launcherを使うクラウド接続も別途未検証です。実環境テストはcheckoutのCLIをプロセス内で実行します。

Azure/R2は今回クラウド試験を再実行していません。既存設定を保持し、設定検査と通常テストで確認しました。以前の実環境結果は [Azure/R2の記録](2026-10-06-live-results.md) にあります。

AWS基盤・IAM・保持設定は変更していません。本番バケット、他run、許可prefix外への実I/Oも行っていません。試験のローカル生成物と証跡は保持しています。実行方法は [実環境テスト手順](live-storage.md) を参照してください。

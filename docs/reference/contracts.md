# 設定とデータの取り決め

この文書は、`tkn-object-storage-catalog` の設定、ファイルの形式、識別子、同期の判定規則をまとめたリファレンスです。
導入と日常の使い方は [README](../../README.md) を参照します。

## 1. 設定

### 1.1. スキーマ バージョン

設定ファイルは、データとは別のスキーマ バージョンを持ちます。
現在の設定スキーマは `"3.0.0"`、ノートは `"2.0.0"` です。カタログと同期記録のデータスキーマは `"1.0.0"` を維持します。

- 各設定ファイルに、引用符で囲んだ `schema_version` が必要です。
- `3.0.x` を読み込みます。旧形式の `1.0.x` は `images` source に正規化し、`2.0.x` は既存の source 名を保持します。旧形式の保存先の既定値は従来の `~/.tkn/azure_blob_note/` 配下です。それ以外の版はエラーになります。
- 未知のキー、重複した YAML キー、型の誤り、URL に含まれる認証情報やクエリ文字列はエラーになります。
- 設定の読み込みが、設定ファイルを書き換えることはありません。

### 1.2. 優先順位

設定は次の順に読み込み、後のものが前のものを上書きします。

1. パッケージに同梱した既定値
2. `~/.tkn/object_storage_catalog/config.yaml`（存在しない場合だけ `~/.tkn/azure_blob_note/config.yaml`）
3. コマンドを実行したフォルダーの `./.tkn/config.yaml`
4. `--config FILE` で明示したファイル
5. 対応する CLI オプション（`--data-root`、`--state-root`、`--notes-root`、`import` の `--convert` / `--no-convert`）

各ファイルは個別に検証します。
優先順位の低いファイルに誤りがある場合、上書きする側が正しくてもエラーになります。
2 と 3 のファイルは、存在しなければ読み飛ばします。
`--config` で指定したファイルが存在しない場合はエラーになります。

`sources` は source ID をキーにしたマッピングです。
新形式で後のファイルに `sources` を書くと、前の一覧全体を置き換えます。
各 source の省略項目には、組み込みの既定値を適用します。
旧形式どうしの設定は、従来どおり項目単位で上書きします。
CLI の保存先・変換オプションは、選択した source だけを上書きします。

source が1つなら自動選択し、複数ならデータ操作に `--source <id>` が必要です。
`config list` は選択なしで全 source を表示できます。
`config.sources` が接続先の一覧、`loaded_sources` が設定ファイルの読み込み履歴、
`origins` が値ごとの決定元、`selected_source` が選択した source ID です。
source ID は1〜64文字の小文字英数字・アンダースコア・ハイフンで、先頭は英数字にします。
Windows の予約名（`con` など）は使えません。

1コンテナー／バケットは1 source に対応します。
同じ接続先のコンテナー／バケットを複数の source に設定すると、prefix が違ってもエラーになります。
Azure はアカウント URL とコンテナー、S3/R2 は provider・エンドポイント・バケットで接続先を区別します。
AWS のリージョンや認証プロファイルだけを変えても別のバケットとは扱いません。
`.env` ファイルは読み込みません。

有効な設定値と、各値の由来は `tkn-object-storage-catalog config list` で確認できます。

### 1.3. パスの解決

- `~` は、現在のユーザーのホーム フォルダーに展開します。
- 相対パスは、コマンドを実行したフォルダーを基準に解決します。スケジュール実行では絶対パスを推奨します。
- source ごとの `data_root` と `state_root` は、互いに重ならない別のフォルダーにします。
- 異なる source のデータ・状態・ノート保存先は、同じフォルダーにも親子のフォルダーにもできません。
- `notes_root` は、`data_root` 内の `staging`、`originals`、`releases`、`catalog`、`provenance`、`history`、および `state_root` と重ねられません。

### 1.4. 設定キー

`schema_version` と `sources` は最上位に書きます。
次の表の保存先・接続先・配信・変換のキーは、すべて `sources.<source-id>.` 以下です。

| キー | 既定値 | 意味 |
| --- | --- | --- |
| `schema_version` | `"3.0.0"` | 設定ファイルごとに必須です。 |
| `sources` | `my-obj-storage-1` の1 source | 利用者が付けた source ID をキーとする設定一覧です。ひな形には2つ目の source のコメント例もあります。 |
| `data_root` | `~/.tkn/object_storage_catalog/data/<source-id>` | 画像とノートを保存する領域です。 |
| `state_root` | `~/.tkn/object_storage_catalog/state/<source-id>` | 同期の基準、実行記録、ログを保存する領域です。 |
| `notes_root` | `null` | 画像ノートの保存先です。`null` の場合は `<data_root>/notes` になります。 |
| `provider` | `azure` | `azure`、`s3`、`r2`。選んだ接続設定だけを使用します。 |
| `s3.bucket` | `null` | S3/R2 の既存の汎用バケット名。接続する場合に必須です。 |
| `s3.region` | `null` | S3 は SDK の既定リージョンを使用。R2 は省略または `auto` です。 |
| `s3.endpoint_url` | `null` | R2 ではアカウントの S3 API HTTPS エンドポイントが必須です。AWS S3 は通常省略します。 |
| `s3.prefix` | 空 | バケット内の同期対象範囲です。 |
| `s3.profile` | `null` | AWS SDK の共有認証プロファイル名。省略時は標準の認証情報探索を使います。 |
| `s3.timeout_seconds` | `60` | 接続とソケット読み取りのタイムアウト。1回の実行全体の制限ではありません。 |
| `azure.account_url` | `null` | ストレージ アカウントの HTTPS エンドポイントです。パスやクエリは含めません。 |
| `azure.container` | `null` | 既存のコンテナーの名前です。Azure に接続するコマンドで必須です。 |
| `azure.prefix` | 空 | コンテナー内の、同期の対象とする相対フォルダーです。 |
| `azure.auth` | `azure_cli` | `azure_cli` または `managed_identity` です。 |
| `azure.managed_identity_client_id` | `null` | ユーザー割り当てマネージド ID を使う場合のクライアント ID です。 |
| `azure.timeout_seconds` | `60` | 正の秒数です。サーバー、ソケット、Azure CLI による認証のそれぞれに適用します。1回の実行全体の制限時間ではありません。 |
| `delivery.url_base` | `null` | 設定したプレフィックスに対応する配信 URL です。 |
| `delivery.cache_control` | `null` | アップロード時に設定する Cache-Control です。`null` の場合は、既存のオブジェクトの設定を保ちます。 |
| `conversion.enabled` | `true` | 静止画の JPEG と PNG を WebP に変換します。 |
| `conversion.format` | `webp` | 変換先の形式です。`webp` だけに対応しています。 |
| `conversion.quality` | `82` | 0 から 100 の整数です。 |
| `conversion.lossless` | `false` | 可逆圧縮の WebP で出力します。 |
| `conversion.strip_metadata` | `true` | 変換後の画像から EXIF、XMP、ICC のメタデータを除きます。 |

`data_root` と `state_root` の省略または `null` は、source ID ごとの既定値を使います。
`notes_root: null` は、解決済みの `data_root` の下にある `notes` です。
旧形式から手動で書き直す際、既存のデータを使い続ける場合は従来の保存先を明示します。
設定の読み込みは、データの移動や設定ファイルの書き換えを行いません。

### 1.5. 画像の変換

- 変換の対象は、静止画の JPEG と PNG です。
- アニメーション PNG は変換せずにコピーします。
- GIF、APNG、AVIF、BMP、ICO、SVG、TIFF、および既存の WebP は、変更せずにコピーします。拡張子は大文字と小文字を区別せずに判定します。
- エンコードの前に、EXIF の向きの情報を画像に適用します。透過は保持します。
- `conversion.strip_metadata` で変換後の画像からメタデータを除いた場合も、原本はすべてのバイトを保持します。
- 変換の条件は、変換の設定、Pillow のバージョン、WebP ライブラリのバージョンから計算した指紋として記録します。この指紋が変わると、`build` は公開用画像を作り直します。
- 変換の有無が変わり、既存の公開用画像の拡張子が変わる場合、`build` は停止します。別の名前で取り込み直します。

## 2. Object Storage の範囲と URL

### 2.1. オブジェクトキーへの対応

`prefix: collection` の場合、公開用画像 `photos/example.webp` は オブジェクト `collection/photos/example.webp` に対応します。
プレフィックスの境界にはスラッシュを含みます。
`collection` を対象にしても、`collection-other` は含まれません。

### 2.2. ノートに書き込む URL

`delivery.url_base` は、設定した範囲にそのまま対応します。
たとえば `https://img.example.com/collection` を設定すると、URL は `https://img.example.com/collection/photos/example.webp` になります。

- この設定は、URL に到達できることや、公開されていることを保証しません。
- `objectUrl` は署名なしの API URL、`objectKey` は prefix を含むキー、`storageProvider` は接続先の種類です。
- `delivery.url_base` が未設定の場合、Azure/S3 は API URL を `url` に使います。アクセスには認証が必要な場合があります。S3 は設定の `region` を URL に使い、省略時はグローバルエンドポイントを使います。
- R2 の API URL は画像配信用ではないため、配信 URL 未設定時の `url` は `null` です。手元の画像へのリンクは引き続き使えます。
- URL のパス部分は、パーセント エンコードします。
- SAS、署名付き URL、Access Key / Secret Key をノートに書き込みません。

### 2.3. 公開範囲の確認

CLI は、コンテナーやバケットのアクセス権を変更しません。
Azure は次のいずれかに当てはまる場合、変更を伴うアップロードの前に確認を求めます。

- `delivery.url_base` を設定している。
- コンテナーが `$web` である。
- コンテナーに匿名アクセスが設定されている。
- コンテナーのアクセス レベルを確認できない。

`$web` コンテナーのアクセス レベルを非公開にしても、静的 Web サイトのエンドポイントは非公開になりません。
詳しくは [Azure Storage の静的 Web サイトにおけるアクセス レベルの影響](https://learn.microsoft.com/azure/storage/blobs/storage-blob-static-website#impact-of-setting-the-access-level-on-the-web-container)を参照します。

S3/R2 は、ポリシー・ACL・独自ドメインなどのすべての公開経路をこの API だけでは判定できないため、公開状態を不明として扱います。
変更を伴う `push` では確認を求め、非対話実行では `--yes` が必要です。
`--yes` は公開状態を変えず、競合も解決しません。

### 2.4. S3/R2 の転送方式

- 一覧は ListObjectsV2 のページを最後まで取得し、対象画像を HEAD してメタデータを取得します。
- 新規 PUT は `If-None-Match: *`、更新 PUT と GET は `If-Match` を使います。条件が成立しない場合は停止し、無条件の再送へ切り替えません。
- PUT には Content-MD5 を付けます。転送後は応答の ETag で HEAD/GET を条件付け、SHA-256 が手元と一致してから同期済みにします。
- 画像1つのアップロード上限は 5,000,000,000 bytes です。条件付きの単一 PUT を使い、multipart upload は実装していません。
- S3 の Directory bucket / S3 Express、Access Point ARN は対象外です。
- 認証は Boto3 のプロファイルまたは標準探索です。CLI は独自の認証キャッシュやキー保存を行いません。
- SDK 設定／環境変数によるエンドポイントの上書きを無効にし、CLI の `s3.endpoint_url` または AWS の標準エンドポイントを使います。
- R2 の region は `auto`。EU / FedRAMP 管轄別の S3 API エンドポイントにも対応します。
- 既存のユーザーメタデータを保持し、CLI の asset_id / sha256 を更新します。Cache-Control の扱いは Azure と共通です。

API の根拠は [Cloudflare の S3 互換性](https://developers.cloudflare.com/r2/api/s3/api/)と [Boto3 PutObject](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html)を参照します。

## 3. アセットとノートの識別

### 3.1. カタログと原本

カタログの記録は `catalog/<asset_id>.json` に保存します。
記録には、`schema_version`、アセット ID、ノート ID、公開用画像とノートの相対パス、作成と更新の日時、原本の情報、現在の公開用画像のハッシュ値と変換の条件が含まれます。
日時は、オフセットを明示した UTC で記録します。
人が読むためのタイトルは、識別には使いません。

原本は `originals/<sha256>/<元のファイル名>` に保存します。
SHA-256 はバイト列を識別し、アセット ID は管理対象の画像をファイル名から独立して識別します。
保存した原本は編集しません。
内容の異なる画像が、同じ公開用画像のパスに重なる取り込みは拒否します。

### 3.2. ノートの項目の担当

| 担当 | 項目・内容 |
| --- | --- |
| 利用者（Obsidian で編集） | `type`、`title`、`category`、`description`、`tags`、`nouns`、`domains`、`projects`、独自に追加したプロパティ、自動生成ブロックの外の本文 |
| CLI | `schemaVersion`、`assetId`、`noteId`、`localPath`、`releaseRef`、`sourceAvailable`、`sourceRef`、`originalRef`、`sourceSha256`、`sha256`、`bytes`、`storageProvider`、`objectKey`、`objectUrl`、`url`、`cover`、`updated`、`syncStatus` |

- CLI が書き込む Frontmatter は、入れ子のない平坦な構造です。利用者が追加したプロパティは、平坦化も削除もしません。
- 新しく作成するノートの `type` は `image` です。
- 新しく作成するノートは、`title` に拡張子を除いたファイル名、`category` に公開用画像の親フォルダーの相対パスを設定します。

### 3.3. 自動生成ブロック

自動生成ブロックは、HTML コメント `object-storage-catalog:begin` と `object-storage-catalog:end` で囲んだ範囲です。
ノートの更新は、このブロックだけを置き換えます。
ブロックのない本文は保持し、末尾に新しいブロックを追加します。
旧 `azure-blob-note:begin` / `azure-blob-note:end` ブロックは、次の更新で同じ位置の新しいブロックに置き換えます。新旧の混在や不完全なマーカーはエラーになります。
ノート schemaVersion 1.0.0 は更新時に 2.0.0 へ変換し、旧 CLI 管理項目 blobName / blobUrl を objectKey / objectUrl に置き換えます。ID と人が書いた項目・本文は保持します。

次の場合、ノートの更新は停止します。

- 同じノート ID またはアセット ID を持つノートが複数ある。
- ノートを作成する予定のパスに、別のアセットのノートがある。
- ノートの `schemaVersion` に対応していない。
- ブロックの開始と終了のコメントが壊れている。

`notes_root` の中で名前を変更または移動したノートは、ID で見つけます。
`notes_root` の外へ移動する場合は、設定を変更するか、ノートを戻す必要があります。

### 3.4. `syncStatus` と `sourceAvailable`

`syncStatus: synced` は、最後に完了した転送の記録です。
現在のクラウドの状態を表すものではありません。
現在の状態は `status --remote` または `verify --remote` で確認します。

原本を持たないアセットは `sourceAvailable: false` になります。
これは、`pull` で クラウドから新しく取り込んだ画像と、`pull` で内容を置き換えた既存のアセットが該当します。
変換済みの画像をダウンロードしても、変換前の原本は復元されません。

## 4. 同期と競合の扱い

### 4.1. 同期の基準

データ保存領域と同期先（Azure はアカウント・コンテナー・prefix、S3/R2 は provider・エンドポイント・バケット・prefix）の組み合わせごとに、同期の基準を `state_root` に保存します。
基準には、アセット ID、オブジェクトの相対名、最後に同期した内容の SHA-256、ETag、サービスのバージョン ID（ある場合）、同期した日時が含まれます。

ETag は、クラウド側の変更の検出に使います。内容のハッシュ値ではありません。
クラウド側のメタデータに記録されたハッシュ値は、内容が同じである根拠として扱いません。
同期の記録がないオブジェクトや、変更されたオブジェクトは、内容を読み取ってハッシュ値を計算してから扱います。

Azure の同期先の指紋と、アセット ID 生成の名前空間は旧版から維持します。既存の同期履歴や ID を変更しません。S3 と R2 は provider を含めて識別し、別クラウドの基準を流用しません。

### 4.2. 判定規則

| 状況 | 動作 |
| --- | --- |
| 手元とクラウド の内容が同じ | 転送せず、同期済みとして記録します。 |
| 手元だけが変わり、クラウド側は基準から変わっていない | `push` でアップロードできます。 |
| クラウド側だけが変わり、手元は基準と一致している | `pull` でダウンロードできます。 |
| 両方が変わっている、または同期の記録がない転送先に異なる内容がある | 競合として停止します。 |
| 片方にファイルがない | 削除としては扱いません。もう一方を削除することはありません。 |

- `--overwrite` は、コマンドの方向に沿って競合を解決します。内容を置き換える場合、対話できない環境では `--yes` も必要です。
- 更新には ETag による条件を付け、新規のアップロードは既存のオブジェクトを上書きしない方法で行います。同時に別の書き込みがあった場合は、後から書いた側が勝つのではなく、競合として停止します。
- `pull` は、ETag による条件付きでダウンロードし、取得したバイト列を検証し、それまでの手元の公開用画像を `history` に退避してから、新しいファイルを書き込みます。

### 4.3. 拒否する名前

次の名前やパスは拒否します。
CLI が、これらのオブジェクトの名前を自動で変更することはありません。

- Windows の予約名
- 上位フォルダーへの参照（`..`）、絶対パス
- バックスラッシュを含む オブジェクトキー
- 末尾がドットまたは空白の名前
- リンク（シンボリック リンクやジャンクション）を経由する管理対象のパス
- 大文字と小文字だけが異なる、クラウド側のパス

## 5. 処理の記録、失敗、バックアップ

### 5.1. 実行ごとの記録

データを変更する実行は、次の3つを作成します。

| 保存先 | 内容 |
| --- | --- |
| `data/provenance/<run_id>.json` | 処理の進行に合わせて追記する記録です。原本と公開用画像の対応、ツールのバージョン、source ID、選択した source の設定の指紋、操作、ハッシュ値、日時を含みます。 |
| `<state_root>/runs/<run_id>.json` | 実行の報告です。 |
| `<state_root>/logs/<run_id>.log` | UTF-8 のログです。 |

- 公開用画像を置き換える前に、準備した内容を記録します。
- データ保存領域ごとに、OS のロックで同時実行を防ぎます。別の実行が進行中の場合、後から始めたコマンドは停止します。
- `--dry-run` では、これらの記録を作成しません。

これらは、このツールの構造化した記録です。
RDF 文書や、汎用のグラフ索引ではありません。
将来、[PROV-O の Entity、Activity、Agent の関係](https://www.w3.org/TR/prov-o/)として書き出すための境界として設計しています。

### 5.2. 中断と復旧

1つのファイルの書き込みは、同じフォルダーの一時ファイルに書いてから置き換える方法で行います。
複数のファイルを扱う1回の実行全体は、途中で中断することがあります。
失敗した記録と、実行中のまま残った記録は、後から確認できます。

`recover` は、記録された公開用画像と原本のハッシュ値が、手元のファイルと正確に一致する場合に限り、カタログとノートへの反映を完了させます。
存在しないバイト列を補うことや、クラウドへの書き込みは行いません。
復旧後に、失敗したコマンドをもう一度実行します。
ノートの読み取りが引き続き失敗する場合は、そのノートを修正するか退避してから再実行します。

### 5.3. バックアップ

- 各 source の `data_root` と `state_root` の全体、および外部に置いた `notes_root` をバックアップします。
- `history` と `originals` は、独立したバックアップの代わりにはなりません。
- 削除や保存期間の管理は自動化していません。
- このツールは、生成 AI のサービスを呼び出しません。

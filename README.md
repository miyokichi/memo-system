# memo-system

AIエージェント向けの記憶システムです。モデルの重みを更新せず、業務経験、根拠、原則、概念、事実、手順、関係を SQLite に保存します。MCP の検索で現在使える知識を取り出し、LLM が生成した変更案はレビューを経て反映します。

## 起動

Python 3.11 以上で、リポジトリのルートからインストールします。

```sh
python -m venv .venv
.venv/bin/python -m pip install .
```

MCP クライアントの stdio 設定例です。パスを実環境に合わせて変更してください。

```json
{
  "mcpServers": {
    "memo-system": {
      "command": "/absolute/path/to/memo-system/.venv/bin/memo-system",
      "env": {
        "MEMO_DB_PATH": "/absolute/path/to/internal-data/memo.db"
      }
    }
  }
}
```

CLI の既定の保存先は `~/.local/share/memo-system/memo.db` です。承認者は信頼できるクライアントから、**同じ DB** を指定して `args: ["--reviewer"]` 付きの別の MCP サーバープロセスに接続します。通常のプロセスでは `memo_review` を公開しません。この区別はローカルで起動するプロセスの設定によるものです。社内で運用するときは、DB ファイルやレビュー用クライアントを操作できる OS アカウントを管理してください。複数テナント向けのユーザー認証・認可は提供していません。

### OpenAI 互換API

LLMを呼ぶツールを使う場合は、MCP プロセスに次の環境変数を渡します。

| 変数 | 内容 |
| --- | --- |
| `MEMO_LLM_BASE_URL` | API のベースURL。例 `https://api.openai.com/v1` |
| `MEMO_LLM_MODEL` | 接続先のモデル名 |
| `MEMO_LLM_API_KEY` | 接続先が要求する場合に設定する Bearer トークン |

サーバーは `POST <base URL>/chat/completions` を使用し、応答のJSONを型検証します。LLMに渡るデータには、入力に加えて関連する社内知識や失効理由が含まれる場合があります。社内データを扱える接続先を指定し、APIキーやDBファイルは公開リポジトリ外で管理してください。検索、登録・承認、手順の制約チェック、評価集計は LLM 接続なしで動きます。

## MCPツール

| 用途 | ツール | 動作 |
| --- | --- | --- |
| 抽象化 | `memo_suggest(task="abstract", ...)` | 関連経験を検索し、条件・因果仮説・根拠付きの原則を提案 |
| 分化 | `memo_suggest(task="differentiate", ...)` | 反例を比較し、概念の分割や例外、識別質問を提案 |
| 具体化 | `memo_concretize`、`memo_verify_plan` | 現在の状況から手順案を生成し、適用条件と構造化された行動制約を確認 |
| 更新 | `memo_suggest(task="update", ...)` | 差分と変更範囲を提案。審査後、版を作成し依存先への影響を伝播 |
| 忘却 | `memo_suggest(task="forget", ...)`、`memo_health`、`memo_history` | 廃止案、期限・依存状態、履歴・置換関係を確認 |
| 検証 | `memo_suggest(task="validate", ...)`、`memo_evaluate` | 検証観点を提案し、独立した評価ケースの観測結果を採点 |
| 知識管理 | `memo_search`、`memo_submit`、`memo_proposals`、`memo_review` | 検索、変更案の登録、保留・却下・承認 |

変更操作は `add`、`revise`、`revise_scope`、`merge`、`split`、`invalidate`、`restore` です。関係も記録の一種として `add` で登録します。承認はバッチ全体が1つのトランザクションです。提出時の `expected_epoch` には検索や提案に含まれる `epoch` を渡します。それ以降に知識が変わった場合は、最新の状態で変更案を作り直します。

`scope` は属性ごとの許容値リストで、異なる属性は AND、同じ属性内の値は OR です。`exceptions` は適用除外の条件を列挙します。属性が不足して適用できるか判断できない知識は、通常の検索から外れます。`include_unknown: true` で確認候補として取得できます。知識の有効期間は `valid_from` 以上、`valid_until` 未満です。時刻はタイムゾーン付きで指定します。

### 最初の原則を登録する例

1. `memo_search` で `epoch` を取得します。
2. 下の引数を使って `memo_submit` を呼びます。例は空のDBを想定しています。
3. 承認者は `memo_proposals` で内容を確認し、独立した評価ケースや根拠を照合して `memo_review` に `proposal_id` と `review` を渡します。原則の採用には `evidence`、`scope`、`consistency`、`transfer`、`regression` の確認が必要です。
4. `memo_search` に `{"kind":"principle","context":{"job":"official-spec"}}` を渡します。同じコンテキストと `goal: "正式仕様確認"` で `memo_concretize` を呼び、手順案を作成できます。目的の表記が異なる場合は `principle_ids: ["official-spec-rule"]` を渡してください。

```json
{
  "expected_epoch": 0,
  "proposal": {
    "reason": "承認済みの仕様管理規則を登録する",
    "operations": [
      {
        "op": "add",
        "record": {
          "id": "quality-policy",
          "title": "仕様管理規則",
          "payload": {
            "kind": "evidence",
            "source": "品質保証部の仕様管理規則",
            "source_version": "1",
            "authority": "品質保証部",
            "assertion": "正式仕様は発効中の承認済み資料から確認する",
            "approved": true
          }
        }
      },
      {
        "op": "add",
        "record": {
          "id": "official-spec-rule",
          "title": "正式仕様は承認済み資料を参照",
          "scope": {"job": ["official-spec"]},
          "payload": {
            "kind": "principle",
            "goal": "正式仕様確認",
            "directive": "対象製品の承認済み資料を確認する",
            "rationale_hypothesis": "未承認の資料は後から変更される可能性がある",
            "required_actions": ["verify_approval", "verify_version"],
            "forbidden_actions": ["use_unapproved"]
          }
        }
      },
      {
        "op": "add",
        "record": {
          "id": "official-spec-source",
          "title": "正式仕様の根拠",
          "payload": {
            "kind": "relation",
            "source_id": "official-spec-rule",
            "target_id": "quality-policy",
            "relation": "supported_by"
          }
        }
      }
    ]
  }
}
```

`memo_review` の `review` には `decision`（`approve` / `hold` / `reject`）、`findings`、`verified_checks` を渡します。`findings` に確認した資料や評価ケースの識別子を残すと、判断の経緯を追跡できます。経験は `experience` 型で状況・目的・判断・行動・結果を登録し、原則と `derived_from` で結びます。根拠は別途 `evidence` として登録します。

### 更新・忘却・評価

- `revise_scope` は指定範囲だけを新しい記録に移します。たとえば新世代製品向けにシステムBへの移行を提案しても、旧世代向けのシステムAは適用できます。新しい記録には別IDと根拠を付けます。
- `depends_on` は必須の前提、`supported_by` は独立した承認済み根拠です。根拠の一つが改訂・失効しても、別の有効な根拠が同じ状況で使える間は検索できます。変更済みの根拠との関係を再利用するには、その関係を明示的に改訂して審査します。
- 適用範囲が重なる失効知識と同一内容の再登録を拒否します。原則では、同じ目的で失効時の経験・根拠だけを再利用する案も拒否します。別の情報を引用した言い換えの判定は、提案時の失効記録と承認者による照合に依存します。
- `memo_evaluate` は、人または別の評価基盤が作成したケースと実行観測を受け取ります。各ケースに `group`、`context`、期待する行動・知識・質問の要否を指定し、観測に実際の `actions`、`used_knowledge`（`id` と `version`）、`asked` を記録します。転移ケースは学習データから分離してください。評価は現在の知識スナップショットを基準に、未知案件正答率、旧知識誤用率など7指標と分母を返します。
- 手順の `constraints_passed` は構造化された条件、根拠の利用可否、必須・禁止行動に対するチェック結果です。自然文の意味や外部システムの権限は、呼び出し側で確認します。手順を実行する機能や定期学習のスケジューラは含みません。

## テスト

```sh
.venv/bin/python -m unittest discover -s tests -v
```

テストは一時DBと模擬LLM応答を使用します。MCPの統合テストではstdioで実際に接続し、提案の提出と承認、検索を確認します。

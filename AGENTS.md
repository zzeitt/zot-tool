# zot-tool

Single-file Python CLI (`scripts/zot.py`) for Zotero library management via pyzotero + Zotero API v3.

## Run

```bash
python3 scripts/zot.py <command> [args]
alias zot="python3 scripts/zot.py"
```

## Required env vars (checked at startup — script exits if missing)

- `ZOTERO_API_KEY`
- `ZOTERO_LIBRARY_ID`
- `ZOTERO_FORBIDDEN_COLLECTION` — 🙊Personal collection key; all its sub-collections/items are excluded
- `ZOTERO_MISC_COLLECTION` — fallback collection for unmatched archive items

## Optional env vars

- `ZOTERO_ARCHIVE_TRIGGER` — default `【归档到Zotero】`
- `ZOTERO_WEBDAV_URL` / `ZOTERO_WEBDAV_USER` / `ZOTERO_WEBDAV_PASS` — WebDAV for offline ZIP upload
- `ZOTERO_OFFLINE_DIR` — local fallback dir for offline HTML (default `/tmp/zotero-offline`)

## Key conventions

- All new items auto-tagged `/unread`; remove when processed
- Forbidden collection (🙊Personal) and all its sub-collections are always excluded from search/list
- Tag matching: prefer existing tags (no spaces), fallback format `#领域-子领域🤖` or `#领域🤖` (max 3)
- Offline archive uses `monolith` to save HTML; `scripts/upload-skill.sh` updates the OpenCLI skill

## Dependencies

- `pyzotero` — install via pip if not present
- `monolith` — for HTML archive saving (called via subprocess)

---

## Commit message format

遵循 [Conventional Commits](https://www.conventionalcommits.org/)：

```
<type>(<scope>): <subject>

[body — 仅在需要补充上下文时使用]
```

| type | 用途 |
|------|------|
| `feat` | 新功能 / 新命令 |
| `fix` | 修复 bug |
| `refactor` | 重构（行为不变） |
| `docs` | 纯文档变更 |
| `chore` | 其他（依赖更新、脚本等） |

**scope** 按代码层区分：

| scope | 对应 |
|-------|------|
| `zot` | `scripts/zot.py` 逻辑变更 |
| `skill` | `SKILL.md` 文档 / skill 定义变更 |
| `zot,skill` | 两者同时改动（如新功能伴随文档更新） |
| `hook` | pre-commit 等钩子脚本 |

**示例**：
```
feat(zot): add tag CRUD commands

Changes
-------
* Add tags_list, tags_add, tags_remove, tags_set functions
* Add CLI dispatch for tags, tag add/remove/set subcommands
* Preserve backward compatibility: zot tag <query> still searches

Context
-------
* Problem: no way to add/remove/replace tags via CLI; tag
  updates required manual Zotero client interaction
* Approach: _tags_update() handles add/remove/set modes via
  zot.update_item(); new zot tags <key> for read
```

**Body 模板**（仅在需要解释 why/how 时使用）：

```
Changes
-------
* <action> <what> [detail]
  Use precise verbs: Add, Remove, Fix, Rewrite, Extract, Bump

Context
-------
* Problem: <what issue or requirement prompted this change>
* Previous state: <what existed before>
* New approach: <why this approach is better>
* Rationale: <non-obvious design decisions or trade-offs>

Considerations
---------------
* Backward compatibility: <what existing behavior is preserved>
* Version impact: <MAJOR/MINOR/PATCH bump>
* Edge cases: <boundary conditions to verify>
* Cleanup: <dead code or stale docs to remove>
```

## Version numbering

版本号记录在 `SKILL.md` frontmatter 的 `version:` 字段。

格式：`MAJOR.MINOR.PATCH`

| 位 | 含义 | 示例 |
|----|------|------|
| MAJOR | 重大重构 / 不兼容变更 | 当前固定为 `1` |
| MINOR | 新功能 / 新子命令 | `1.9.0` → `1.10.0` |
| PATCH | 同版本内 bug 修复 | `1.8.1` → `1.8.2` |

规则：
- 每次合并 feature PR 时递增版本号
- 如 feature 分支有多个 commit，仅在最终合并时标注版本
- PATCH 修复可在 commit message 尾部标注（如 `(v1.9.1)`），也可不标

## Pre-commit hook

安装方式（首次克隆后执行一次）：

```bash
# Unix / Git Bash
cp scripts/pre-commit .git/hooks/pre-commit
cp scripts/commit-msg  .git/hooks/commit-msg
chmod +x .git/hooks/pre-commit .git/hooks/commit-msg
```

```powershell
# PowerShell
Copy-Item scripts\pre-commit .git\hooks\pre-commit
Copy-Item scripts\commit-msg  .git\hooks\commit-msg
```

### pre-commit

扫描 staged diff，拦截以下涉密内容：

hook 会检查提交内容中是否包含：
- API key（`ZOTERO_API_KEY` 赋值）
- Nutstore / WebDAV 个人路径
- Collection key 值（非变量名，即 8 位大写字母数字串出现在上下文中）
- WebDAV 用户名 / 密码赋值

被拦截时输出具体问题行，拒绝提交。紧急绕过：`git commit --no-verify`。

### commit-msg

验证 commit message 格式符合 [Conventional Commits](#commit-message-format)：

| 检查项 | 规则 |
|--------|------|
| 格式 | `type(scope): subject` |
| type | 必须在 `feat/fix/refactor/docs/chore/test/style/perf/ci/build/revert` 中 |
| scope | 必须在 `zot/skill/hook` 中（多 scope 用逗号，如 `zot,skill`） |
| 长度 | subject ≤ 50 字符 |
| 尾标点 | subject 不得以 `.!?;` 结尾 |
| 空行 | subject 和 body 之间必须有空行 |
| body 宽度 | 每行 ≤ 72 字符 |
| `@` 残留 | 拦截 subject 首字符或 body 末行为 `@` 的情况（Bash here-string 误用） |

紧急绕过：`git commit --no-verify`。

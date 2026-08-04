# zot-tool

Zotero 文献库命令行管理工具，基于 pyzotero + Zotero Web API v3。

```bash
zot <noun> <verb> [args]
```

## 命令树

```
zot
├── item
│   ├── add     <type> <title> <url> <coll> [json]   手动创建条目
│   ├── remove  <key>                                 删除条目
│   ├── list    [n]                                   最近条目（dateAdded 降序）
│   ├── search  <query> [n]                           全文搜索（相关性排序）
│   └── archive <url> [title] [#tag]...               智能归档
│          --no-offline                               跳过离线保存
├── tag
│   ├── add     <key> <tag>...                        添加标签（幂等）
│   ├── remove  <key> <tag>...                        移除标签
│   ├── set     <key> [tag]...                        替换全部标签
│   ├── list    <key>                                 列出条目标签
│   └── <query> [n]                                   按标签搜索（向后兼容）
├── coll
│   ├── list                                          列出所有 Collection
│   ├── remove <key>                                  删除 Collection（非空警告）
│   └── <name>                                        查找 Collection（整词匹配）
├── note
│   ├── add     <key> [content]                       LLM 摘要（支持 pipe）
│   └── set     <key> [content]                       直接写入（支持 pipe）
├── attachment
│   ├── add     <key> <file> [name]                   上传附件（需 WebDAV）
│   ├── remove  <child-key>                           删除子条目
│   ├── update  <att-key> <file> [name]               原地更新附件
│   └── list    <parent-key>                          列出子条目
└── help                                              帮助
```

### Alias（旧命令，向后兼容）

| 旧命令 | 等同于 | 旧命令 | 等同于 |
|--------|--------|--------|--------|
| `zot search` | `item search` | `zot tags` | `tag list` |
| `zot archive` | `item archive` | `zot addnote` | `note add` |
| `zot add` | `item add` | `zot setnote` | `note set` |
| `zot delete` | `item remove` | `zot attach` | `attachment add` |
| `zot list` | `item list` | `zot attachments` | `attachment list` |
| `zot collections` | `coll list` | `zot detach` | `attachment remove` |
| | | `zot reattach` | `attachment update` |

## 快速开始

```bash
# 必填
export ZOTERO_API_KEY="your_key"
export ZOTERO_LIBRARY_ID="your_id"
export ZOTERO_FORBIDDEN_COLLECTION="key"   # 🙊Personal 的 key
export ZOTERO_MISC_COLLECTION="key"        # Misc 根 collection 的 key

# 可选
export ZOTERO_LIBRARY_TYPE="group"         # 默认 user，group library 时设 group
export ZOTERO_WEBDAV_URL="https://..."     # 附件/离线副本上传
export ZOTERO_WEBDAV_USER="..."
export ZOTERO_WEBDAV_PASS="..."

# 使用
alias zot="python3 scripts/zot.py"
zot item search "machine learning"
zot tag "/unread"
zot coll "pi"
```

## 核心工作流

```bash
# 归档 —— 自动抓标题、匹配 Collection、打 tag、离线副本、LLM 摘要
zot item archive "https://example.com/article"

# 搜索
zot item search "transformer attention"

# 未读
zot tag "/unread"

# 找 Collection
zot coll "machine-learning"

# 读完去掉 /unread
zot tag remove KC5ETPXM "/unread"

# 加标签
zot tag add KC5ETPXM "#AI🤖" "#paper📄"

# 上传附件
zot attachment add KC5ETPXM paper.pdf

# 修离线 HTML
zot attachment update A1B2C3D4 fixed.html

# 查看子条目
zot attachment list KC5ETPXM

# 删子条目
zot attachment remove A1B2C3D4

# 删条目
zot item remove KC5ETPXM
```

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `ZOTERO_API_KEY` | ✅ | — | Zotero API key |
| `ZOTERO_LIBRARY_ID` | ✅ | — | Library ID |
| `ZOTERO_LIBRARY_TYPE` | 否 | `user` | `user` 或 `group` |
| `ZOTERO_FORBIDDEN_COLLECTION` | ✅ | — | 🙊Personal 的 key |
| `ZOTERO_MISC_COLLECTION` | ✅ | — | Misc 根 collection 的 key |
| `ZOTERO_WEBDAV_URL` | 否 | — | WebDAV 端点 |
| `ZOTERO_WEBDAV_USER` | 否 | — | WebDAV 用户名 |
| `ZOTERO_WEBDAV_PASS` | 否 | — | WebDAV 密码 |
| `ZOTERO_ARCHIVE_TRIGGER` | 否 | `【归档到Zotero】` | AI 触发词 |
| `ZOTERO_OFFLINE_DIR` | 否 | 系统 temp | 无 WebDAV 时本地保存 |

## 约定

- 🚫 `🙊Personal` 及其子 Collection 始终排除
- 📌 新条目自动打 `/unread`，处理后手动移除
- 🏷️ Tag：`#关键词🤖`，无空格，最多 3 个，优先匹配库内已有
- 🔤 排序：`item search` → 相关性；`item list` / `coll` → dateAdded 降序
- 📁 Archive 自动识别：HTML → monolith；PDF/EPUB → 直下
- 📎 附件存 WebDAV，ZIP + `.prop` 侧车

## 测试

```bash
python -m pytest tests/ -v              # 全部（需 API key）
python -m pytest tests/test_unit.py -v  # 仅纯逻辑（无网络）
```

详见 `tests/README.md`。

## License

MIT

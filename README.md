# zot-tool

Zotero 文献库命令行管理工具，基于 pyzotero 库开发。

## 功能特性

- 🔍 **高级搜索**：按标题、标签、Collection 搜索
- 📁 **Collection 管理**：自动匹配、最佳归类
- 🏷️ **智能标签**：模糊匹配已有标签，emoji 增强辨识度
- 📦 **离线归档**：自动保存网页离线副本到 WebDAV
- 📝 **LLM 摘要**：自动生成中文内容提纲
- 🔒 **隐私保护**：自动排除 🙊Personal 收藏夹

## 快速开始

```bash
# 克隆仓库
git clone https://github.com/zzeitt/zot-tool.git
cd zot-tool

# 设置环境变量
export ZOTERO_API_KEY="your_api_key"
export ZOTERO_LIBRARY_ID="your_library_id"
export ZOTERO_FORBIDDEN_COLLECTION="your_forbidden_collection_key"
export ZOTERO_MISC_COLLECTION="your_misc_collection_key"

# 使用
python3 scripts/zot.py search "machine learning"
python3 scripts/zot.py list
python3 scripts/zot.py archive "https://example.com/article"
```

## 核心命令

| 命令 | 说明 |
|------|------|
| `zot search <query>` | 搜索条目 |
| `zot tag <tag>` | 按标签筛选 |
| `zot coll <name>` | 按 Collection 筛选 |
| `zot list [limit]` | 列出最近条目 |
| `zot add <type> "<title>" <url>` | 添加条目 |
| `zot archive <url>` | 智能归档 URL |
| `zot delete <key>` | 删除条目 |

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `ZOTERO_API_KEY` | ✅ | Zotero API Key |
| `ZOTERO_LIBRARY_ID` | ✅ | Library ID |
| `ZOTERO_FORBIDDEN_COLLECTION` | ✅ | 隐私 Collection Key |
| `ZOTERO_MISC_COLLECTION` | ✅ | Misc Collection Key |
| `ZOTERO_WEBDAV_*` | 否 | WebDAV 离线存储 |
| `ZOTERO_ARCHIVE_TRIGGER` | 否 | 归档触发词 |

## 标签约定

- 新条目自动带上 `/unread` 标签
- 优先匹配已有标签（无空格）
- 最多 3 个标签，带 emoji 后缀

## License

MIT

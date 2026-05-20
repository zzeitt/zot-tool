---
name: zot-tool
description: Zotero 文献库命令行管理工具
version: 1.4.1
---
# Zot Tool - Zotero 文献管理工具

Zotero 文献库命令行管理工具，支持高级搜索、标签过滤、Collection 管理，并自动排除 🙊Personal 隐私内容。

## 环境要求

- `ZOTERO_API_KEY` 环境变量已设置
- `ZOTERO_LIBRARY_ID` 环境变量已设置（你的 Zotero Library ID）
- Library Type: user

## 核心命令

### 搜索功能
```bash
zot emacs [limit]        # 搜索 emacs 相关内容
zot search <query> [limit]   # 一般关键词搜索
zot tag <tag> [limit]        # 按标签搜索
zot coll <collection> [limit] # 按 Collection 搜索
```

### 浏览功能
```bash
zot list [limit]         # 列出最近条目
zot collections          # 列出所有 Collections
```

### 条目管理
```bash
zot add <type> "<title>" <url> <coll> [extra]   # 添加新条目（自动带上 /unread 标签）
zot archive <url> [hint]                        # 智能归档
zot archive --no-offline <url>                  # 归档但不保存离线副本
zot addnote <item-key> [content]                # 添加 LLM 生成摘要的 Note
zot delete <item-key>                           # 删除条目
```

## 归档工作流

当检测到提示词包含归档触发词（默认 `【归档到Zotero】`）时，自动触发归档流程。

### 归档流程
1. 提取 URL
2. 抓取标题、描述、平台类型
3. 推断合适的 itemType 和 tags
4. 在已有 Collections 中查找最佳匹配，无匹配则创建 Misc--xxx 子集合
5. 自动添加 /unread 标签
6. 用 monolith 保存离线 HTML 副本
7. 将离线文件作为附件上传到该条目

## 标签约定

- **优先使用本库中已存在的标签**（模糊匹配，禁止用带空格的 tag）
- **新生成 tag 格式**：`#领域-子领域🤖` 或 `#领域🤖`（带 `#` 前缀 + emoji 后缀）
  - 例如：`#AI-ML🤖`、`#编程💻`、`#经济💰`、`#advice🔗`
  - 最多 3 个 tag，优先取最相关的
- emoji 映射规则（`scripts/zot.py` 中的 `_emoji_for_tag`）：
  - 🤖 AI/机器学习 | 💰 经济金融 | 💻 编程开发 | 🔢 数学统计 | 🤔 哲学逻辑
  - 🎙️ 播客 | 📺 视频 | 📚 教程 | 🛠️ 工具 | 📄 论文 | 📖 书籍 | 📜 历史
  - 🔬 科学 | 🏥 健康 | 🌍 政治社会 | 🎨 艺术设计 | 🎮 游戏 | 📊 数据
  - 🎵 音乐 | 🖼️ 图像 | 🔒 安全隐私 | 🌐 网络 | 💼 商业 | 🏠 房产
  - ✍️ 写作 | 🌱 生活 | 其他 → 🔗
- **fallback**：无法匹配预设规则时，取标题第一个有意义的英文词作为 tag

### /unread 标签
**所有新添加的条目必须自动带上 /unread 标签。** 这是本库的核心约定：
- 新建条目 → 自动附加 `{"tag": "/unread", "type": 1}`
- 读完/处理完后 → 手动移除 `/unread` 标签

## 隐私保护

- 🙊Personal collection 及其所有子 collection 被完全排除

## 工具脚本

脚本位置：`scripts/zot.py`（与 SKILL.md 同级目录）
```bash
python3 scripts/zot.py <command>
# 或创建别名
alias zot="python3 scripts/zot.py"
```

## 实现细节

- 基于 `pyzotero` 库
- 使用 Zotero API v3
- 缓存 forbidden items 避免重复请求
- 支持递归排除子 collection
- pyzotero 版本需 ≥1.11.0（不支持旧版 `timeout=` 参数）


---
name: zot-tool
description: Zotero 文献库命令行管理工具
version: 1.7.1
---
# Zot Tool - Zotero 文献管理工具

Zotero 文献库命令行管理工具，支持高级搜索、标签过滤、Collection 管理，并自动排除 🙊Personal 隐私内容。

## 环境要求

- `ZOTERO_API_KEY` 环境变量已设置
- `ZOTERO_LIBRARY_ID` 环境变量已设置（你的 Zotero Library ID）
- Library Type: user
- `ZOTERO_WEBDAV_URL/USER/PASS`：坚果云 WebDAV 端点及凭证（附件上传必需）

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
zot archive <url> ["title-hint"] [#tag1] [#tag2]   # 智能归档（自动识别 HTML/二进制文件）
zot archive --no-offline <url> ["title-hint] [#tag1]  # 归档但不保存离线副本
zot addnote <item-key> [content]                # 添加 LLM 生成摘要的 Note
zot delete <item-key>                           # 删除条目
```

## 归档工作流

当检测到提示词包含归档触发词（默认 `【归档到Zotero】`）时，自动触发归档流程。

### 归档流程
1. 提取 URL
2. 抓取标题、描述、平台类型
3. **Cloudflare 反爬检测**（v1.7.1+）：若 curl 拿到 `Attention Required` 等 CF 拦截页，输出 `⚠️ CLOUDFLARE_BLOCKED` 标记，让 AI 看到后用 browser fallback 补抓正文
4. 推断合适的 itemType 和 tags
5. 在已有 Collections 中查找最佳匹配（**多信号评分**，v1.7.0+），无匹配则创建 Misc--xxx 子集合
6. 自动添加 /unread 标签
7. 用户提供的 #tag 建议（若有）优先，未满 3 个时由 infer_tags 补足
8. **自动识别文件类型**：
   - 二进制文件（PDF/EPUB 等）→ 直接下载原始文件，上传到 WebDAV
   - HTML 网页 → 用 monolith 抓取离线副本，上传到 WebDAV
9. 将离线文件作为 Zotero attachment item（linkMode: imported_file）关联到条目

### Cloudflare 反爬场景处理（v1.7.1+）
- **问题**：部分网站（如 johndcook.com）用 Cloudflare 拦截 curl user-agent，导致 `fetch_url_metadata` 拿到 "Attention Required" 页面，description 为空
- **检测**：v1.7.1 在 `fetch_url_metadata` 里检查 `Attention Required` / `cf-error-code` 等关键字，标记 `cf_blocked: true`
- **fallback**：archive_url 输出 `⚠️ CLOUDFLARE_BLOCKED` 标记。AI 看到后：
  1. 用 `browser_use navigate` + `execute_js` 拿 body text
  2. 用 `pyzotero` 更新 item 的 note，把 body text 写进去
- **当前边界**：fallback 仍依赖 AI 主动处理，未做到完全自动化

### Collection 匹配策略（v1.7.0 重构）
**问题**：早期版本用 4 字符前缀子串匹配 → "transferable" 误匹配 "transformer"；完全忽略 coll 已收录的内容信号。

**新策略**——三维信号评分，阈值 ≥ 3 才匹配：
1. **coll 名字关键词** ∩ text 关键词 → **+1/词**（整词匹配，保留 math↔mathematics 缩写映射）
2. **coll 内容关键词**（最近 20 条 items 的 title+abstract）∩ text 关键词 → **+2/词**（强信号）
3. 缓存：单次 bulk zot.items() 拉取所有 items 本地分组，签名缓存 5 分钟 TTL

**典型场景**：
- Quanta 神经科学文章 → 既有 `Misc--neuroscience`（内容已含 brain/memory/neural 等词）→ 稳稳匹配
- 全新主题文章（如 Rust 入门）→ 无任何 coll 匹配 → 新建 `Misc--rust/xxx`（保留你主动建 coll 的习惯）

### 二进制文件识别规则
- URL path 以 `.pdf`/`.epub`/`.mobi`/`.docx` 等扩展名结尾
- 已知二进制站点（LibGen、booksdl 等）的 `get.php` 下载链接
- LibGen 格式：`https://libgen.li/ads.php?md5=...`（对应下载链接自动识别）

### 不保存离线副本
- 二进制文件无 WebDAV 配置时跳过离线保存
- `--no-offline` 参数可强制跳过

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
- 二进制附件通过 `_detect_binary_url` + `archive_binary_url` + `save_file_attachment` 自动处理
- HTML 附件通过 `monolith` 抓取，经 `_upload_to_webdav` 上传
- 所有附件均使用 `linkMode: imported_file`，ZIP 格式，附带 XML `.prop` 文件
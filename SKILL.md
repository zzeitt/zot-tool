---
name: zot-tool
description: Zotero 文献库命令行管理工具
version: 1.11.0
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
zot setnote <item-key> [content]                # 设置原始 Note 内容（不经过 LLM）
zot delete <item-key>                           # 删除条目
zot cleanup-empty-collections                   # 删除所有空的子 collection (v1.8.0)
```

### 附件管理 (v1.9.0)
```bash
zot attach <item-key> <file> [name]             # 添加附件（上传到 WebDAV）
zot attachments <item-key>                       # 列出所有子条目（attachment + note）
zot detach <child-key>                           # 删除指定子条目（attachment 或 note）
zot reattach <att-key> <file> [name]             # 原地更新附件文件内容（保留 key，Zotero 客户端感知版本变化
```

**工作流**：先用 `zot attachments <parent-key>` 查看所有子条目（含 note），再选择性 `detach` 删除或 `reattach` 替换附件。note 清理现在也可以通过 `attachments` 查看 → `detach <note-key>` 完成。

### Tag 管理 (v1.10.0)
```bash
zot tags <item-key>                              # 列出某条目的所有 tags
zot tag add <item-key> <tag1> [tag2] ...         # 添加 tag(s)（幂等，不重复添加）
zot tag remove <item-key> <tag1> [tag2] ...      # 移除指定 tag(s)
zot tag set <item-key> <tag1> [tag2] ...         # 替换全部 tags（不传 tag 则清空）
```

**与旧搜索命令的兼容**：`zot tag <query> [limit]`（第一个参数不是 add/remove/set 时）仍触发按 tag 搜索。

## 归档工作流

当检测到提示词包含归档触发词（默认 `【归档到Zotero】`）时，自动触发归档流程。

### 归档流程
1. 提取 URL
2. 抓取标题、描述、平台类型
3. **Cloudflare 反爬检测**（v1.7.1+）：若 curl 拿到 `Attention Required` 等 CF 拦截页，输出 `⚠️ CLOUDFLARE_BLOCKED` 标记，让 AI 看到后用 browser fallback 补抓正文
4. 推断合适的 itemType 和 tags
5. 在已有 Collections 中查找最佳匹配（**v1.8.0+: 域名硬映射 → 多信号评分（v1.7.0+）**，无匹配则创建 Misc--xxx 子集合）
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

### 已知平台域名硬映射（v1.8.0 关键修复）
**问题（2026-07-07 微信公众号事件）**：
库内**已存在** `Misc--wechat` (6TXTXUMC)，但 v1.7.4 archive 微信公众号 URL 时，description 为空 → `find_best_collection` 早返回 None → 落到 `create_misc_subcollection` 的 URL-slug/title 分支 → 创建了中文长名 coll `Misc--一文看懂ai推理芯片和训练芯片的区别`（需要事后清理）。

**根因**：v1.7.2 已把 `weixin.qq.com → wechat` 的域名硬映射**接到 create_misc_subcollection 的命名逻辑**，但**没接到 find_best_collection 的匹配流程**。域名命中只能"创建"已有 coll，不能"匹配"已有 coll。

**v1.8.0 修复**：
1. 新增模块级 `DOMAIN_TO_SUBCOLL` 字典，覆盖 17 个已知平台：
   - 中文：`mp.weixin.qq.com→wechat` / `chaspark.com→chaspark` / `bilibili.com→bilibili` / `xiaohongshu.com→xhs` / `zhihu.com→zhihu` / `juejin.cn→juejin`
   - 开发者：`github.com→github` / `arxiv.org→arxiv` / `ycombinator.com→hn` / `stackoverflow.com→stackoverflow` / `medium.com→medium` / `substack.com→substack`
   - 音视频：`youtube.com→youtube` / `podcasts.apple.com→podcast` / `open.spotify.com→spotify`
   - 百科：`wikipedia.org→wikipedia`
2. 新增 `_domain_subcoll_name(url)` 提取器
3. 新增 `_find_existing_domain_collection(url)`：**先**在库内查 `Misc--<sub>` 是否已存在，**命中则直接返回** coll_key（绕过多信号评分）
4. `archive_url` 调用顺序：**域名硬映射 → 多信号评分 → create_misc_subcollection**
5. 重构 `create_misc_subcollection` 用 `_domain_subcoll_name` + `_fallback_*` 双路径
6. `_all_collections()`：v1.7.4 SKILL.md 承诺的分页 + 5 分钟缓存 helper 落地（`zot.everything(zot.collections())`）
7. 新增 `cleanup-empty-collections` CLI 命令（v1.8.0）：扫描所有子 collection，跳过 🙊Personal/📢Public/Misc 根，**自动删除**空 coll（用 raw API 拿 version + If-Unmodified-Since-Version DELETE）

**为什么 `cleanup-empty-collections` 必须存在**：
v1.7.4 之前空 coll 永久累积（GLOBAL.md 已记 2026-07-06 踩坑），v1.8.0 域名硬映射修复后，**新建 coll 前会先查已存在**，空 coll 不会再被新建；但历史积累的 55 个空 coll 还得手动清。新命令把这件事变成 `zot cleanup-empty-collections` 一行。

**典型场景（v1.8.0 验证）**：
- 微信公众号 → 库内 `Misc--wechat` 命中 → 不再创建中文长名 coll ✓
- 茶思屋 chaspark.com → 库内 `Misc--chaspark` 命中 ✓
- Bilibili / 知乎 / 小红书 / 掘金 → 各自的 `Misc--<sub>` 命中 ✓
- 全库 516 → 369 colls（清掉 55 个空 coll）→ 0 empty ✓

### 离线副本 Visual Sanity Check（v1.7.3+）
- **触发**：每次 `zot archive` 成功保存 monolith HTML 后，输出 `📸 VISUAL_CHECK_READY: /var/minis/offloads/<file>.html`
- **AI 处理流程**（必做）：
  1. 用 `browser_use navigate` 打开该路径（minis:// 形式）
  2. `screenshot full_page=true` 抓全页截图
  3. **视觉分析**：
     - CSS 是否完整（卡片、代码块、字体、配色）
     - 板式是否正常（标题、段落、间距）
     - 是否有缺图/错位
     - 数学公式、交互组件是否保留
  4. 异常时主动重抓或提示用户
- **为什么必要**：之前的感知机文章归档后用户报告 CSS 缺失，本地浏览器打开其实是好的——是 Zotero 客户端渲染 bug。提前发现能避免上传错误文件。

### 微信公众号 HTML 后处理（v1.8.1 → **v1.8.2 数据丢失修复**）

**v1.8.2 修复了 v1.8.1 的严重数据丢失 bug**：之前替换 `<img data-src="URL" src="data:image/...;base64,...">` 时把 src 的真实 base64 内嵌图（1.5MB / 张）替换成 data-src 的 100 字符外链 URL，导致 **20.9MB 文章变成 322KB、丢失全部 28 张内嵌图、Zotero 离线看不到图**。

- **问题**：微信公众号文章由 monolith 保存后，正文被 `visibility: hidden; opacity: 0;` 隐藏，图片使用 `data-src` 懒加载——两者都依赖 JavaScript。Zotero 禁用 JS，导致正文完全空白。
- **修复**（v1.8.2 修正）：`save_offline_copy` 在 monolith 完成后自动调用 `_fix_wechat_html()` 对 HTML 做后处理：
  1. 移除 `#js_content` 上的 `visibility: hidden; opacity: 0;` 内联样式
  2. **保留** `src="data:image/...;base64,..."`（**真实图数据，不是占位符**），只删 `data-src` 属性
  3. 删 `data-aistatus / data-imgfileid / data-s / data-ratio / data-type / data-w` 等 WeChat 内部调试属性（~8KB 噪音）
- **函数签名**：`_fix_wechat_html(filepath)`，返回 `True` 表示做了修改，`False` 表示无需处理（非微信文章或已修复）
- **幂等性**：通过检测特征字符串 `id="js_content"` 判断是否需要处理，重复调用安全
- **已知局限**：微信 CDN 图片（`mmbiz.qpic.cn`）的 `data-src` 在 Zotero 内仍可能因网络策略无法加载，但**内嵌 base64 图全保留**，离线 Zotero 仍能看到所有图

### Collection 匹配策略（v1.7.0 重构 + v1.7.4 关键修复 + **v1.8.0 域名硬映射优先级最高**）

**v1.8.0 新增**：匹配流程的最外层先跑**域名硬映射**（见下一节），命中 `Misc--<sub>` 直接 return；不命中才走下面这套多信号评分。**已知平台永远不会被这套评分"误判"**。

**问题**：
- 早期版本用 4 字符前缀子串匹配 → "transferable" 误匹配 "transformer"
- 完全忽略 coll 已收录的内容信号
- **v1.7.4 发现的更严重 bug**：`zot.collections()` 默认 limit=100，但用户的库有 707 colls，旧代码只看了前 100 条（14%），86% 的 coll 完全没参与匹配。即使 `Misc--pi/π` 已经存在，旧代码也看不到！
- **v1.8.0 修复**：`_all_collections()` 用 `zot.everything()` 真正分页拉完所有 colls + 5 分钟 TTL 缓存（之前 SKILL.md 已承诺但代码缺失）。

**新策略**——三维信号评分，阈值 ≥ 3 才匹配：
1. **coll 名字关键词** ∩ text 关键词 → **+1/词**（整词匹配，保留 math↔mathematics 缩写映射）
2. **coll 内容关键词**（最近 20 条 items 的 title+abstract）∩ text 关键词 → **+2/词**（强信号）
3. 缓存：单次 bulk zot.items() 拉取所有 items 本地分组，签名缓存 5 分钟 TTL

**两遍扫描**（v1.7.2 性能优化）：先用 name 评分筛出 top 5 候选（无 API 调用），再 fetch content signature。
**希腊字母支持**（v1.7.4）：regex 包含 `\u0370-\u03ff\u1f00-\u1fff`，否则 `Misc--pi/π` 里的 `π` 永远无法匹配。
**分页拉取所有 colls**（v1.7.4→v1.8.0 落地）：`_all_collections()` 替代 `zot.collections()`，分页拉完 + 5 分钟缓存。

**典型场景**：
- 微信公众号 → **域名硬映射** → 库内 `Misc--wechat` 命中（v1.8.0）✓
- Quanta 神经科学文章 → 多信号评分命中 `Misc--neuroscience`（content 已含 brain/memory/neural 等词）
- John D. Cook "A crank formula for π" → 多信号评分命中 `Misc--pi/π`（content 已含 π/transcendental 等词）
- 全新主题文章（如 Rust 入门）→ 无任何 coll 匹配 → 新建 `Misc--rust/xxx`（保留你主动建 coll 的习惯）

### Subcollection 命名策略（v1.7.2 重构 + v1.8.0 与 DOMAIN_TO_SUBCOLL 统一）
**问题**：旧实现 `create_misc_subcollection(title + " " + description)` 只取标题前 2 个词，对"标题党"文章（如 "The Smallest Brain You Can Build" 关于 perceptron）会错配成 `Misc--smallest/brain`。

**v1.8.0 重构**：`create_misc_subcollection` 拆出三个独立函数，命名逻辑只走一个权威路径：
- `_domain_subcoll_name(url)` — 已知平台域名硬映射（17 个平台，与匹配流程共享同一张表）
- `_fallback_sub_name_from_url(url)` — 未知域名时取主域第一段
- `_fallback_sub_name_from_title(name_hint)` — title 文本取前 2 个有意义 token

**新策略**——多信号优先级：
1. **已知平台域名**（最高优先）：调用 `_domain_subcoll_name(url)`，覆盖 `weixin.qq.com→wechat` / `chaspark.com→chaspark` / `github.com→github` / `arxiv.org→arxiv` / `bilibili.com→bilibili` / `xhs→xhs` / `zhihu→zhihu` / `juejin→juejin` / `hn→hn` / `stackoverflow→stackoverflow` / `medium→medium` / `substack→substack` / `youtube→youtube` / `podcast→podcast` / `spotify→spotify` / `wikipedia→wikipedia` 等
2. **URL 主域第一段**（未知域名兜底）
3. **title 词**（非 URL 时）：如 `perceptron-explained-from-scratch` → `perceptron/scratch`
4. **用户提供的 #tag**（可选）：如 `#感知机` → `感知机`

**停用词过滤**（`_MISC_NAMING_STOPWORDS`）：过滤"blog/post/article/explained/build"等无信息量词，避免 `Misc--introduction/xxx`、`Misc--build/neural` 这类名字。

**典型场景**：
- `mp.weixin.qq.com/s/abc123` → 域名硬映射 → `Misc--wechat` ✓
- `chaspark.com/xxx` → 域名硬映射 → `Misc--chaspark` ✓
- `ranpara.net/posts/perceptron-explained-from-scratch/` + `#感知机` → 未知域名 + URL slug → `Misc--perceptron/scratch`
- `johndcook.com/blog/2026/06/06/from-kepler-to-bessel/` + `#math` → 未知域名 + URL slug → `Misc--kepler/bessel`
- `github.com/zzeitt/zot-tool` → 域名硬映射 → `Misc--github` ✓

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

## 版本历史

### v1.11.0 — 附件原地更新（In-place Update）

`reattach` 不再 "删旧 + 建新"，改为原地更新文件内容并保留 attachment key：
- `_upload_to_webdav` 新增 `existing_key` 参数——传入时 PATCH 现有 item 的 md5/mtime/filename，复用原有 key 上传新的 ZIP 和 .prop
- Zotero 客户端 sync 时检测到 mtime/hash 变化，作为文件更新处理而非全新 item
- WebDAV 上旧 ZIP/.prop 被新 PUT 自然覆盖，无需手动清理

### v1.10.0 — Tag 增删改查

新增四个 tag 管理命令，补全 tag CRUD：
- `zot tags <key>` — 列出条目的所有 tags
- `zot tag add <key> <tag>...` — 添加 tag(s)，幂等不重复
- `zot tag remove <key> <tag>...` — 移除指定 tag(s)
- `zot tag set <key> <tag>...` — 替换全部 tags（不传 tag 则清空）

内部实现：`_tags_update()` 统一处理 add/remove/set 三种模式，通过 `zot.update_item()` 写入。
向后兼容：`zot tag <query>` 仍然触发按 tag 搜索。

### v1.9.0 — 附件与 Note 子条目管理

新增三个子条目管理命令，支持细粒度控制（不再 "一刀切删除全部附件"）：
- `zot attachments <parent-key>` — 列出父条目下**所有子条目**（attachment + note），含类型标记、note 内容预览
- `zot detach <child-key>` — 删除指定子条目（attachment 或 note 均可），attachment 自动清理 WebDAV
- `zot reattach <att-key> <file> [name]` — 替换指定附件（v1.11.0 起改为原地更新，保留 key）

工作流：先用 `attachments` 查看所有子条目 → 选择性 `detach` 删除（含 note）或用 `reattach` 替换附件。
解决了旧 note 无法清理导致重复累积的问题。

### v1.8.3 — Note 生成质量修复

**问题（v1.8.2 及之前）**：`fetch_url_metadata` 拿不到 description 时（微信公众号、Wikipedia、部分博客等），`_create_content_note` 直接跳过 LLM，输出**"两行 URL 标题"的垃圾 note**——用户称之为"两行 url 一样的无用信息 note"。讽刺的是：LLM API 已配置好但完全没用上。

**修复（v1.8.3）**：
1. **`_llm_summarize` 支持空 description**：新增 fallback mode prompt，基于标题+URL+类型推断内容（"预期核心议题 / 平台与定位 / 阅读建议"）
2. **`_create_content_note` 不再跳过 LLM**：无论 description 是否为空，都调 `_llm_summarize`
3. **`_build_minimal_fallback_note` 全新 helper**：LLM 完全失败时仍生成 metadata-rich note + 明确"未生成摘要"标记 + 修复建议（运行 `zot addnote <item-key>` 重试）

**实测**：archive Wikipedia 文章（description 为空）自动生成 555 chars 的 rich LLM 摘要，含 Grothendieck 1966 菲尔兹奖、隐居经历等准确信号——**不是 prompt 回声**。
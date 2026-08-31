---
name: zot-tool
description: Zotero 文献库命令行管理工具
version: 2.3.5
---
# Zot Tool - Zotero 文献管理工具

Zotero 文献库命令行管理工具，支持高级搜索、标签过滤、Collection 管理，并自动排除 🙊Personal 隐私内容。

## 环境要求

- `ZOTERO_API_KEY` 环境变量已设置
- `ZOTERO_LIBRARY_ID` 环境变量已设置（你的 Zotero Library ID）
- Library Type: user
- `ZOTERO_WEBDAV_URL/USER/PASS`：坚果云 WebDAV 端点及凭证（附件上传必需）

### Windows 环境注意事项

Windows 上使用 monolith 有若干已知问题，以下是规避方案。

**monolith `-o` 参数 bug（v2.10.1）**

monolith 2.10.1 在 Windows 上使用 `-o` 参数会 panic（`src\main.rs:291`，错误 `Os { code: 3, kind: NotFound }`）。这是 monolith 自身的 bug，与 Rust 标准库文件创建逻辑有关。

**解决方案**：不传 `-o`，改用 stdout 重定向：

```powershell
monolith <url> > output.html
```

zot.py 内部调用 `subprocess.run(["monolith", "-o", outfile, url])`，Windows `CreateProcess` 只自动搜索 `.exe` 扩展名。如果需要透明包装 monolith（不改 zot.py），编译一个 `.exe` wrapper 拦截 `-o` 参数并转为 stdout 重定向即可。关键点：

- wrapper 必须是 PE `.exe` 格式（`.bat`/`.cmd` 不会被 `subprocess.run` 以 `shell=False` 发现）
- monolith 的 stderr 输出是未内联资源的 URL 列表，**不能**重定向到输出文件（否则 HTML 顶部会有 URL 噪音）；应丢弃或单独记录

**Python 3.14 兼容性**

`import pyzotero` 在 Python 3.14 上可能因 `whenever` 时区库兼容性问题而挂起。若遇到此问题，可降级到 Python 3.12 或等待 pyzotero 上游修复。

## 核心命令

### 搜索与浏览 —— 在文献库中查找和浏览条目

| 命令 | 说明 | 场景 |
|---|---|---|
| `zot item search <query> [limit]` | 全文搜索，Zotero 相关性排序 | 按关键词查找文献 |
| `zot tag <query> [limit]` | 按标签筛选条目 | 查找所有带 `/unread` 标签的未读条目 |
| `zot coll <name>` | 按 Collection 名称查找（整词匹配，显示 key + 条目数） | 查找某个主题 Collection |
| `zot coll list` | 列出所有 Collections | 了解库中有哪些分类 |
| `zot coll remove <key>` | 删除指定 Collection（非空时警告） | 清理废弃 Collection |
| `zot item list [limit]` | 最近添加的条目，**dateAdded 降序** | 查看最近归档了什么 |

> **Aliases**: `zot search` → `item search` | `zot collections` → `coll list` | `zot list` → `item list` | `zot tags <k>` → `tag list <k>`
> **排序说明**：`item list` 和 `coll` 按 Zotero 默认的 dateAdded 降序排列。`item search` 由 Zotero 相关性算法排序。所有命令均已使用 `_all_collections()` 分页拉取全部 collection。

### 条目管理 —— 创建、归档、删除条目

| 命令 | 说明 | 场景 |
|---|---|---|
| `zot item archive <url> [title] [#tag]...` | **最常用命令**：智能归档 URL | 归档网页文章、PDF 链接、播客等 |
| `zot item archive --no-offline <url> [title]` | 同上，但跳过离线 HTML 保存 | 只需要条目元数据 |
| `zot item add <type> <title> <url> <coll> [extra]` | 手动添加条目（自动打 `/unread`） | 非 URL 来源或特殊类型条目 |
| `zot item remove <item-key>` | 删除条目及其所有子条目 | 重复条目、归档失败清理 |
| `zot note add <item-key> [content]` | 给已有条目追加 LLM 摘要 Note | 归档后补充 AI 摘要 |
| `zot note set <item-key> [content]` | 直接写原始 Note 内容（不调 LLM） | 手动写入自定义笔记 |

> **Aliases**: `zot archive` → `item archive` | `zot add` → `item add` | `zot delete` → `item remove` | `zot addnote` → `note add` | `zot setnote` → `note set`

### 标签管理 —— Tag 增删改查 (v1.10.0)

| 命令 | 说明 | 场景 |
|---|---|---|
| `zot tag add <key> <tag>...` | 添加 tag(s)，已存在则跳过（幂等） | 给条目补充标签 |
| `zot tag remove <key> <tag>...` | 移除指定 tag(s) | 条目读完后去掉 `/unread` |
| `zot tag set <key> <tag>...` | 替换全部 tags（不传 tag = 清空） | 批量重整标签 |
| `zot tag list <key>` | 列出某条目的所有 tags | 查看条目有哪些标签 |

> **两种用法**：`zot tag add/remove/set/list ...` → Tag CRUD；`zot tag <query> [limit]` → 按 tag **搜索**（向后兼容）。Alias: `zot tags <k>` → `tag list <k>`。

### 附件管理 —— 文件上传、查看、替换 (v1.9.0 / v1.11.0)

| 命令 | 说明 | 场景 |
|---|---|---|
| `zot attachment add <key> <file> [name]` | 上传本地文件为 attachment | 手动补充离线文件 |
| `zot attachment list <parent-key>` | 列出父条目下所有子条目（attachment + note） | 查看条目有哪些附件和笔记 |
| `zot attachment remove <child-key>` | 删除指定子条目（attachment 自动清理 WebDAV） | 删除错误的附件或垃圾 note |
| `zot attachment update <att-key> <file> [name]` | **原地更新**附件文件内容，保留 attachment key | 归档后离线 HTML 有问题，替换修复后的文件 |

> **典型工作流**：`zot attachment list <parent-key>` → 找到 attachment key → `zot attachment update <att-key> fixed.html`。
> **Aliases**: `zot attach` / `zot attachments` → `attachment list` | `zot detach` → `attachment remove` | `zot reattach` → `attachment update`
> **向后兼容**：`zot attach <key> <file>` 仍可用（等同 `attachment add`）

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

### Note 生成：双后端分发（v2.1.0）

`zot item archive` 完成后自动生成内容摘要 Note。后端分两种情况：

| 后端 | 触发条件 | 行为 |
|------|---------|------|
| **minis-model-use** | CLI 在 PATH 中 | 直接调 LLM API，同步生成 HTML note 写入 Zotero |
| **Claude/Agent** | minis-model-use 不可用 | 写 pending JSON 到 `$TEMP/zot_pending/note_{key}.json`，**由 AI 异步处理** |

**Claude/Agent 路径处理流程**（AI 必做）：

1. `zot item archive` 输出 `📝 Note generation queued for Claude/agent` 后，检查 `zot_pending/` 目录
2. 读取每个 `note_*.json` 文件，获取 `title`、`source_text`、`item_type`、`url`、`parent_key`
3. 根据内容生成结构化中文 HTML note（模板见下），调用 `zot note set <parent_key> "<html>"`
4. 删除 pending JSON 文件，清理空目录

**源内容优先级**（`_llm_summarize` 内部）：
1. **离线 HTML 正文**（monolith 抓取的完整文章，去标签取前 4000 字）
2. **curl meta description**（页面 `<meta name="description">`，前 2000 字）
3. **标题 + URL only**（无任何内容时的推断模式）

**Note HTML 模板**（AI 生成时参照）：

```html
<h3>📋 基本信息</h3>
<p><strong>标题</strong>：{title}</p>
<p><strong>类型</strong>：{item_type}</p>
<p><strong>URL</strong>：{url}</p>

<h3>🎯 核心结论</h3>
<p>（2-4 句话高度概括核心主张）</p>

<h3>📝 主要观点</h3>
<ol>
<li>…</li>
</ol>

<h3>💡 值得关注的信息</h3>
<p>（1-3 条容易忽略的信号）</p>

<h3>🔍 延伸方向</h3>
<p>（2-3 个深入阅读方向）</p>
```

> **注意**：当 source_text 为空时（极少见），使用"预期内容指南"模式——基于标题+URL+平台类型推断，而非胡编内容。

### 离线 HTML 正文提取（v2.1.0）

`_llm_summarize` 现在优先读取 monolith 离线 HTML 作为摘要源：
- 打开 `_last_offline_file`（由 `save_offline_copy` / `archive_binary_url` 设置）
- 去除 `<script>` / `<style>` / HTML 标签，合并空白
- 取前 4000 字符传给 LLM（覆盖绝大多数文章的核心段落）

相比旧版只用 meta description（通常 ≤ 200 字符），离线正文能提供 20 倍以上的内容信号，让 LLM 摘要更准确。

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
1. 新增模块级 `DOMAIN_TO_SUBCOLL` 字典，覆盖 17+ 个已知平台：
   - 中文：`mp.weixin.qq.com→wechat` / `chaspark.com→chaspark` / `bilibili.com→bilibili` / `xiaohongshu.com→xhs` / `zhihu.com→zhihu` / `juejin.cn→juejin`
   - 开发者：`github.com→github` / `arxiv.org→arxiv` / `ycombinator.com→hn` / `stackoverflow.com→stackoverflow` / `medium.com→medium` / `substack.com→substack`
   - 音视频：`youtube.com→youtube` / `podcasts.apple.com→podcast` / `open.spotify.com→spotify`
   - 百科：`wikipedia.org→wikipedia`
   - 个人博客（v2.3.2 新增）：`infinitelymore.xyz→infinitelymore`（Joel David Hamkins 的 Substack 频道，避免被多信号评分误匹配到《Handbook of Floating-Point Arithmetic》）
   - **科技高管署名博客**：`gatesnotes.com→gatesnotes`（Bill Gates 个人博客，2026-08-31 验证：Cloudflare 反爬 403 导致 fetch_url_metadata 拿到 "Access Denied" → fallback 退化成 `Misc--www` 垃圾命名）
   - **数学/图形深文个人博客（v2.3.5 新增）**：`alanzucconi.com→alanzucconi`（Alan Zucconi 博客，2026-08-31 验证：`xorshift-generators` 51K 字长文被多信号评分误匹配到 `Misc--《The Mystery of the Prime Numbers》`，加进硬映射 → 命中/创建 `Misc--alanzucconi`，已预创建 key `A24ZFB7Q`）
2. 新增 `_domain_subcoll_name(url)` 提取器
3. 新增 `_find_existing_domain_collection(url)`：**先**在库内查 `Misc--<sub>` 是否已存在，**命中则直接返回** coll_key（绕过多信号评分）
4. `archive_url` 调用顺序：**域名硬映射 → 多信号评分 → create_misc_subcollection**
5. 重构 `create_misc_subcollection` 用 `_domain_subcoll_name` + `_fallback_*` 双路径
6. `_all_collections()`：v1.7.4 SKILL.md 承诺的分页 + 5 分钟缓存 helper 落地（`zot.everything(zot.collections())`）
7. 在 v1.8.0 引入了空 collection 批量清理能力（后由 v1.12.0 `zot coll remove <key>` 作为统一的单 collection 删除命令替代）

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
1. **已知平台域名**（最高优先）：调用 `_domain_subcoll_name(url)`，覆盖 `weixin.qq.com→wechat` / `chaspark.com→chaspark` / `github.com→github` / `arxiv.org→arxiv` / `bilibili.com→bilibili` / `xhs→xhs` / `zhihu→zhihu` / `juejin→juejin` / `hn→hn` / `stackoverflow→stackoverflow` / `medium→medium` / `substack→substack` / `youtube→youtube` / `podcast→podcast` / `spotify→spotify` / `wikipedia→wikipedia` / `infinitelymore.xyz→infinitelymore` / `gatesnotes.com→gatesnotes` / `alanzucconi.com→alanzucconi` 等
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

### v2.3.5 — patch 修复

- **`alanzucconi.com → alanzucconi` 域名映射**：Alan Zucconi 个人数学/图形/Unity 教学博客（2026-08-31 验证：`xorshift-generators` 51K 字长文被多信号评分误匹配到 `Misc--《The Mystery of the Prime Numbers》`。加进 `DOMAIN_TO_SUBCOLL` 后未来命中或创建 `Misc--alanzucconi`，已预创建 key `A24ZFB7Q`（item `7F4A6PRZ`））。

### v2.3.4 — patch 修复

- **`barrd.dev → barrd` 域名映射**：Dave 的 Bristol 个人技术博客（2026-08-31 验证）。barrd.dev 描述 "Git worktree ... without stashing or constant checkouts" 被多信号评分误匹配到 Turing《On Computable Numbers, with an Applicatoin to the Entscheidungsproblem》（`without`/`decision` 与 coll name 偶然高分）。加进 `DOMAIN_TO_SUBCOLL` 后未来命中或创建 `Misc--barrd`。

### v2.3.3 — patch 修复

- **`gatesnotes.com → gatesnotes` 域名映射**：Bill Gates 个人博客（2026-08-31 验证）。`gatesnotes.com` 走 Cloudflare 反爬，curl/Python 全部 403，`fetch_url_metadata` 拿到 "Access Denied" 作为标题、description 为空 → 多信号评分无信号命中 → `create_misc_subcollection` 退化 fallback `Misc--www` 垃圾命名。加进 `DOMAIN_TO_SUBCOLL` 后未来命中 `Misc--gatesnotes`（已预创建，key `6MVGEDXC`）。

### v2.3.2 — patch 修复

- **`infinitelymore.xyz → infinitelymore` 域名映射**：Joel David Hamkins 的 Substack 频道（集合论/数学哲学付费 newsletter）此前走多信号评分时会被误匹配到《Handbook of Floating-Point Arithmetic》（2026-08-25 验证）；加进 `DOMAIN_TO_SUBCOLL` 后走硬映射 → 命中或创建 `Misc--infinitelymore`，避免误判。

### v2.3.1 — patch 修复

- **集成测试断言修复**：`search_by_collection` 语义已变，修正 2 个遗留断言
- **auto-release 建 Release**：打 tag 后同一步创建 GitHub Release，绕开 GITHUB_TOKEN 递归限制

### v2.3.0 — 版本号统一

- **Google/Microsoft 域名映射**：`DOMAIN_TO_SUBCOLL` 增加两家域名
- **monolith `-F`**：处理字体重页面；httpx 写超时
- **集成测试修复**：`setnote`→`_note_set`、移除 `search_by_collection` 的 `limit`、CI 加 `requests` 依赖
- **auto-release 单一来源**：tag 由 `SKILL.md` frontmatter `version:` 驱动，不再按 PR 标题推断 bump

### v2.2.0 — 图片压缩

- **note/attachment 内嵌图片压缩**：抽 `_compress_html` 纯函数，note 路径复用压缩，避免写超时
- **单元测试 API-free**：`test_unit.py` 不再依赖 Zotero API

### v2.1.1 — DOMAIN_TO_SUBCOLL netloc 后缀匹配

- 域名匹配改用 netloc 后缀，修复子域名误判

### v2.1.0 — 离线 HTML 源 + agent LLM 路径

- 新增离线 HTML 源与 agent LLM 路径

### v2.0.1 — 平台判断修复

- `sys.platform` 替代 `platform.system()`，修复部分平台 hang

### v2.0.0 — argparse 迁移 + 命令大统一

- **argparse 迁移**：全量替换手写 sys.argv 解析，`zot --help` / `zot item --help` 等各级 help 正常工作
- **16 个旧命令统一为 5 个 noun**：`item` / `tag` / `coll` / `note` / `attachment`，每个 noun 下共享动词（add/remove/list/search 等）
- **forbidden subcollection 修复**：`coll list`、`find_best_collection` 等现在递归屏蔽 `🙊Personal` 所有子孙 collection（之前只屏蔽根和一层子节点）
- **`LIBRARY_TYPE` 环境变量**：支持 `user` / `group` 库类型（`_delete_collection_raw` 之前 hardcode `/users/`）
- **`search_by_collection` 语义修正**：现在返回匹配的 collection 列表 + item count，而非 collection 内的 items
- **word-boundary 匹配**：`coll search "pi"` 匹配 `Misc--pi/π` 但不匹配 `pipeline`
- **`search_emacs` 命令移除**
- **测试套件**：`tests/` 目录，pytest 集成 + 单元测试
- **CI/CD**：`.github/workflows/test.yml`（push/PR/tag 自动测试） + `release.yml`（tag push 自动创建 GitHub Release）

### v1.12.0 — 命令统一（共享动词设计）

所有命令统一为 `zot <noun> <verb>` 结构，与 `tag add/remove/set/list` 保持一致：
- **`item`** 新增：`item add` / `item remove` / `item list` / `item search` / `item archive`（旧 `add` `delete` `list` `search` `archive` 作为 alias 保留）
- **`note`** 新增：`note add` / `note set`（旧 `addnote` `setnote` 作为 alias 保留）
- **`attachment`** 新增：`attachment add` / `attachment remove` / `attachment update` / `attachment list`（旧 `attach` `attachments` `detach` `reattach` 作为 alias 保留，`zot attach <k> <f>` 向后兼容）
- **`coll`** 扩展：`coll list` / `coll remove <key>`（旧 `collections` 作为 alias，`cleanup-empty-collections` 移除）
- **`tag list`** 新增：等同于旧 `zot tags`

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
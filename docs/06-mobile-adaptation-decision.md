# 06 · 移动端（飞书内打开）适配决策

> 结论先行：**飞书不提供可复用的 H5 UI 组件库**，它只是把网页放进 WebView。你现在看到的"没适配"是这 5 个 HTML 页面自己只有桌面栅格导致的。
> **推荐方案 = 响应式改造 + 移动端信息架构重排（同一份文件、断点内切换）**，预计 2–3 人天；小程序重写（2–3 周）与 aPaaS 低代码壳都不划算。

## 1. 现状体检（代码事实）

| 项 | 现状 | 影响 |
| --- | --- | --- |
| viewport | 5 个页面都有 `width=device-width,initial-scale=1` | 元信息没问题，问题在布局 |
| 主栅格 | `.grid{grid-template-columns:252px minmax(420px,1fr) 384px}` | 固有最小宽度 ≈ 1056px |
| 断点 | 只有 `media(max-width:1180px)` 把三列改成 `220px 1fr 320px` | 手机（390–430px）仍走三列 |
| 高度 | `.grid{height:calc(100vh - 50px - 44px - 58px)}` | 移动端地址栏/工具栏变化会截断 |
| 其余页面 | reader.html 有 640px 断点；library/people 有 820/900px 两列；highlights 无断点 | 半适配状态 |
| UA 识别 | 全仓无 `Lark/Feishu/userAgent` 判断 | 没有移动端分支入口 |

**截图成因**：内容固有宽度 1056px，而设备 CSS 宽度约 390px。当内容撑不出视口时，Android WebView 的 overview 模式（`loadWithOverviewMode`）会把整页等比缩小铺满，于是出现"三栏都在、字小到看不清"。这解释了为什么不是横向滚动，而是整体缩微。

## 2. 飞书到底给了什么（调研结论）

### 2.1 网页应用（H5）：只有容器能力，没有 UI
官方文档把网页应用定义为"用 H5 开发、运行在飞书客户端内的应用"，提供的是**容器开放能力 + JSAPI**：

- `web-meta`：定制网页容器表现（可定义网页特性，非网页应用也能用）
- 隐藏导航栏 / 设置导航栏颜色 / 隐藏导航栏按钮
- 设置屏幕方向（强制横竖屏 / 跟随系统）
- 侧滑关闭应用、更多菜单项启停
- JSAPI：登录免登、分享、扫码、选人、地理位置等

→ **这些都不管布局**。文字排版、栅格、触控尺寸仍然是你自己的事。

### 2.2 真正"自带 UI"的三条替代路线

| 路线 | 现成 UI | 自适应机制 | 对你这个项目的代价 |
| --- | --- | --- | --- |
| 飞书小程序（Gadget） | 官方组件库（视图容器/列表/表单/导航栏） | TTSS 的 `rpx`，以 750 为基准自动等比 | 全部页面重写为 ttml/ttss/js，审核发布；多轨音频、悬浮面板、hover 交互都要重做。**2–3 周起** |
| 飞书 aPaaS 低代码 | 页面设计器自带"双端适配"（PC/手机各一套画布） | 平台生成 | 只适合表单/列表/审批壳。你的 Python 声纹+LLM 管线塞不进去，最多做入口页 |
| Semi Design | React 桌面端设计系统 | 栅格 + 断点 | 是"设计规范 + 组件"，不是给单文件 HTML 用的；引入等于用 React 重写，**成本最高** |

→ 所以"飞书有没有内部 UI 给我们用"的答案是：**H5 这条路没有；要么换成小程序/低代码（重写），要么自己在 H5 里把响应式做掉（改造）。**

## 3. 四个候选方案对比

| # | 方案 | 工作量 | 效果 | 风险 |
| --- | --- | --- | --- | --- |
| A | **响应式改造 + 移动端 Tab 重排**（推荐） | 2–3 人天 | 手机可用、PC 不变 | 低。单文件 HTML，无需构建 |
| B | 单独 `/m` 移动版页面（UA 分流） | 3–5 人天 | 移动端体验最好 | 双份代码要同步维护，长期成本高 |
| C | 飞书小程序重写 | 2–3 周 | 最"原生"，可进工作台 | 能力受限、审核、与现有 Python 服务要另开 API |
| D | aPaaS 低代码壳 | 3–5 人天 | 入口像个应用 | 主界面仍是 H5，问题没解决 |

**建议：A 为主，B 作为兜底**（若某个页面实在压不进单列，就只给那一页做 `/m`）。
C 只有在"要长期驻留飞书工作台、需要原生导航/消息能力"时才值得，且应作为 v2 立项，而不是修 bug。

## 4. A 方案的具体做法（index.html 为主）

### 4.1 断点
- `≤1180px`：保留现有（双列收窄）
- `≤960px`：左筛选面板折叠成抽屉，主区两列 → 单列
- `≤720px`：**移动端布局**——顶部只留场次切换 + 更多菜单；主区改为底部 Tab 三段：`筛选 / 记录 / 总结`；播放器吸底
- `≤420px`：字号 15px、间距收紧、隐藏次要列（`.p-time`、`.p-rate`）

### 4.2 关键改动
1. **信息架构先改，再改 CSS**：三栏硬堆成一列会得到一条 5 屏长的页面。移动端必须"一次只看一件事"——Tab 切换，筛选进抽屉。
2. `100vh` → `100dvh`（地址栏伸缩）；`.grid` 在移动端改成 `height:auto` + 页面整体滚动。
3. 触控目标 ≥ 44px；`hover` 才出现的时间戳/操作按钮改成常显或点击态（触屏没有 hover）。
4. 输入框 `font-size:16px`，否则 iOS 聚焦会自动放大页面。
5. `viewport-fit=cover` + `padding-bottom:env(safe-area-inset-bottom)`，避开飞书底部工具栏与 Home 指示条。
6. 时间轴柱体加高到 ≥ 32px、可点区域外扩，否则手指点不中。
7. 保留缩放（不加 `user-scalable=no`），飞书有无障碍要求。
8. 用 `web-meta` 关掉重复的导航栏、设置导航栏配色，能省出约 44px 竖向空间。

### 4.3 可直接起步的骨架

~~~css
/* 移动端：Tab + 抽屉 + 吸底播放器 */
@media (max-width:720px){
  html,body{font-size:15px}
  .topbar{height:auto;flex-wrap:wrap;padding:8px 10px;gap:8px}
  .topbar .gsearch,.topbar .ghost{display:none}      /* 收进"更多"菜单 */
  .grid{display:block;height:auto;min-height:0;padding:8px}
  .grid > .panel{display:none}
  .grid > .panel.tab-on{display:flex}
  .m-tabbar{display:flex;position:sticky;bottom:0;z-index:40;background:#fff;
    border-top:1px solid var(--border);padding-bottom:env(safe-area-inset-bottom)}
  .m-tabbar button{flex:1;min-height:48px;border:0;border-radius:0}
  .m-tabbar button.on{color:var(--accent);font-weight:700;box-shadow:inset 0 2px 0 var(--accent)}
  .filters-drawer{position:fixed;inset:auto 0 0 0;max-height:82vh;overflow:auto;
    background:#fff;border-radius:14px 14px 0 0;transform:translateY(101%);transition:transform .2s}
  .filters-drawer.open{transform:none}
  .p-time,.p-rate{display:none}
}
~~~

配套 JS 只需三段：Tab 切换 `classList`、抽屉开关、把筛选面板在移动端移动到抽屉容器里（`appendChild`，不要复制 DOM，否则事件重复绑定）。

### 4.4 验收
1. Chrome DevTools 设备模拟 390×844、430×932，检查无横向滚动（`document.documentElement.scrollWidth === clientWidth`）。
2. 真机：飞书扫码打开，确认没有被整体缩小——页面顶部临时打一行 `innerWidth` 自检。
3. 关键路径走一遍：切场次 → 调时间段 → 点发言定位播放 → 生成总结 → 导出。
4. 5 个页面 + 实时监听浮层 + 设置抽屉一起看，别只改首页。

## 5. 下一步
- 同意 A：直接改 `index.html`（CSS 断点 + Tab/抽屉 + 吸底播放器），再补 reader/library/people/highlights 的窄屏收尾。
- 若想先验证：只做 index.html 一个页面，半天出可点版本，先在飞书里试。

## 6. 参考
- 网页应用简介（容器能力与 JSAPI）: https://open.larksuite.com/document/client-docs/h5/introduction
- 响应式设计指南: https://open.larkenterprise.com/document/design-specification/responsive-design/responsive-design-guidelines
- 飞书小程序 TTSS/rpx: https://open.feishu.cn/document/client-docs/gadget/framework/ui-layer/ttss
- aPaaS 页面的双端适配: https://ae.feishu.cn/hc/zh-CN/articles/401868177080

## 7. 实施记录（已完成）

### 7.1 断点策略（最终）
| 区间 | 布局 |
| --- | --- |
| ≤1024px | 移动外壳：单栏 + 底部 Tab（记录 / 筛选 / 总结）+ 顶栏"更多"面板 |
| 1025–1600px | 桌面三栏，顶栏紧凑版（场次按钮 250px、隐藏 FTS 角标、徽标单行） |
| >1600px | 桌面三栏，全宽顶栏 |

### 7.2 改动清单
**app/web/index.html**
- 新增 `<nav class="m-tabbar">` + `#mMore` 按钮 + `.m-sheet` 更多面板
- 新增 `mobileShell()`：matchMedia 断点监听、Tab 切换、顶栏按钮在移动/桌面间搬移（`appendChild` + 注释锚点，事件不丢）
- 记录行改为两列网格 `grid-template-areas:"tc spk" "tc body"`，时间常显在左侧
- `100vh` → `100dvh`、`env(safe-area-inset-bottom)`、输入框 16px、触控目标 ≥34px
- 时间轴标题换行，避免与红色时间码重叠
- 桌面：`.brand{flex:none;white-space:nowrap}` 修复 1440 下品牌被压成一字、徽标竖排

**app/web/library.html** — 表格在 ≤760px 转卡片（复选框绝对定位、操作按钮常显、数值行内排列）

**app/web/people.html** — ≤640px 顶栏两行、搜索框满宽、列表不限高

**app/web/reader.html** — ≤640px 顶栏拆两行（品牌+返回 / 操作按钮）、`.p .when` 时间戳常显（触屏无 hover）

**app/web/highlights.html** — 本身在 390px 下可用，未改动

### 7.3 验证
用 `chrome-headless-shell` 在 390×844 / 430×932 / 768×1024 / 1024×768 / 1280×800 / 1440×900 / 1920×1000 逐档截图；
390 下 `document.documentElement.scrollWidth === 390`（无横向溢出）；Tab 点击、更多面板、桌面回归均人工核对。
验证截图未随仓库提供（含真实直播内容）；需要时在本地重新生成。

### 7.4 已知待办
- 飞书真机需再验一次（WebView 的 overview 缩放行为与桌面 Chrome 不同）；若仍被整体缩小，检查是否被容器 `web-meta` 覆盖了 viewport。
- 移动端"实时监听"入口已隐藏（`getDisplayMedia` 在手机上不可用），仅保留回看与检索。


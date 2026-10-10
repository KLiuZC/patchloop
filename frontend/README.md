# patchloop 前端开发指南

[项目首页](../README.md) · [使用指南](../docs/usage.md) · [交互约定](../docs/frontend-behavior.md) · [API 参考](../docs/api.md)

## 模块说明

网页工作台负责任务配置、状态查询、补丁与日志展示、人工审查和发布确认。前端通过 FastAPI 读写任务记录，修复任务由独立 worker 执行。

使用 React、JavaScript 和 Vite，组件为 `.jsx`，请求与数据处理模块为 `.js`。路由采用 React Router，查询缓存采用 TanStack Query；共享组件使用 Radix Select、Motion、Markdown 渲染及代码高亮。依赖与版本以 [package.json](package.json) 和锁文件为准。

## 本地开发

需要 Node.js 22.12+，也支持 20.19+ 的 20.x 版本。以下命令均在**项目根目录**执行。

安装并启动前端：

```bash
npm --prefix frontend ci
npm --prefix frontend run dev
```

在另一个终端启动已配置的任务 API：

```bash
uv run agent serve
```

访问 http://127.0.0.1:5173。浏览历史记录只需要前端与 API；执行修复任务还需启动 worker。后端依赖、模型凭证、Docker 镜像和 worker 启动见[项目快速开始](../README.md#快速开始)。

## 配置

| 配置 | 默认值或位置 | 作用 |
| --- | --- | --- |
| 开发地址 | `http://127.0.0.1:5173` | Vite 开发服务 |
| 预览地址 | `http://127.0.0.1:4173` | 本地检查构建产物 |
| `PATCHLOOP_API_TARGET` | `http://127.0.0.1:8000` | 开发与预览服务的后端代理目标 |
| 本地环境文件 | `frontend/.env.local` | 覆盖开发环境配置，不纳入 Git |
| 代理前缀 | `/api` | 转发到后端时移除该前缀 |

后端地址变化时，准备本地配置文件：

```bash
test -f frontend/.env.local || cp frontend/.env.example frontend/.env.local
```

编辑 `PATCHLOOP_API_TARGET` 后重启前端服务。模型密钥与 GitHub 发布凭证由后端配置，前端不接收或保存凭证。

开发与预览端口被占用时启动失败，可通过 `npm --prefix frontend run dev -- --port 5174` 指定其他端口。代理和端口实现见 [vite.config.js](vite.config.js)。

## 页面与路由

| 路由 | 页面文件 | 职责 |
| --- | --- | --- |
| `/tasks` | [TaskList.jsx](src/pages/TaskList.jsx) | 任务列表、状态筛选、分页与刷新 |
| `/tasks/new` | [NewTask.jsx](src/pages/NewTask.jsx) | 本地仓库任务配置与提交 |
| `/tasks/github` | [GithubTask.jsx](src/pages/GithubTask.jsx) | Issue 导入、仓库准备与关联任务创建 |
| `/tasks/:taskId` | [TaskDetail.jsx](src/pages/TaskDetail.jsx) | 概览、代码改动、执行日志、验证报告与人工审查 |

根路径与未匹配路径跳转到 `/tasks`。路由定义见 [App.jsx](src/App.jsx)，公共导航和页面框架位于 [Layout.jsx](src/components/Layout.jsx)。

## 代码组织

| 目录或模块 | 职责 |
| --- | --- |
| [main.jsx](src/main.jsx) | 路由、查询客户端及动态效果设置 |
| `src/pages/` | 页面组合与流程入口 |
| `src/components/` | 共享表单、内容展示、审查与交付面板 |
| [lib/api.js](src/lib/api.js)、[query-client.js](src/lib/query-client.js) | 请求封装、响应校验、缓存及重试策略 |
| [lib/task.js](src/lib/task.js)、[artifacts.js](src/lib/artifacts.js) | 状态与表单数据、补丁及轨迹解析 |
| [lib/github.js](src/lib/github.js)、[reviews.js](src/lib/reviews.js)、[delivery.js](src/lib/delivery.js)、[publications.js](src/lib/publications.js) | 领域接口、数据核对和恢复规则 |
| `src/lib/*-draft.js` | Issue 流程、待确认审查请求和发布草稿的会话保存 |
| [styles.css](src/styles.css) | 页面样式、响应式布局与可访问性适配 |
| `src/test/`、`*.test.js`、`*.test.jsx` | 页面联调测试、组件及数据处理测试 |

关键组件与行为规则的对应关系见[交互约定中的实现索引](../docs/frontend-behavior.md#实现与测试索引)。

## 开发约定

- 分别展示执行、验证和人工审查状态，当前审查以独立接口为准。
- 查询可以轮询；提交、交付和发布由用户明确触发，结果不明确时按接口约定核对原请求。
- 展示与提交绑定同一组证据；版本或内容变化时重新核对，迟到响应不能回退已知状态。
- 保留输入与复制内容的原始语义，Markdown、日志及命令预览仅用于展示。
- 使用共享组件和直接的操作文案，保持键盘操作、窄屏布局和减少动态效果适配。

请求恢复、会话存储、内容渲染及证据核对的具体要求统一维护在[前端交互约定](../docs/frontend-behavior.md)。

## 测试与构建

```bash
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
npm --prefix frontend run preview
```

测试使用 Vitest、Testing Library 和模拟请求，覆盖页面流程、数据校验、并发响应及组件行为，不调用真实模型或发布 GitHub 内容。修改特定流程时可指定测试文件，例如 `npm --prefix frontend test -- src/test/workbench.test.jsx`。

构建产物位于 `frontend/dist/`。`preview` 用于本地检查，并保留开发代理；正式静态部署需要 SPA 路由回退及 `/api` 反向代理，开发配置不会自动进入静态部署环境。

真实模型及 Docker 联调单独进行，结果见[产品流程验收](../docs/evaluation.md#产品流程验收)，不与模拟请求测试混为同一验证结论。

## 相关文档

| 文档 | 内容 |
| --- | --- |
| [使用指南](../docs/usage.md) | 页面操作、任务配置及异常处理 |
| [前端交互约定](../docs/frontend-behavior.md) | 请求恢复、审查证据和内容展示规则 |
| [API 参考](../docs/api.md) | 接口字段、状态码及服务端恢复约定 |
| [架构与运行机制](../docs/architecture.md) | worker、数据库、产物与执行边界 |
| [评测与验收](../docs/evaluation.md) | 实验与真实流程验收入口 |

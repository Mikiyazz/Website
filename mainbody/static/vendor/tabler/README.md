# Tabler（本地化静态资产）

- 版本：@tabler/core 1.7.3（dist 产物）
- 来源：https://github.com/tabler/tabler → npm 包 `@tabler/core` 的 `dist/` 目录
- 许可证：MIT（Copyright (c) Tabler / Paweł Kuna）
- 文件：`tabler.min.css`、`tabler.min.js` 及其 sourcemap
- 说明：项目要求零 CDN 依赖（校园网/内网离线可用），故仅落盘 dist 编译产物，
  不引入仓库源码与 pnpm 构建链。

升级方式：替换同名文件即可；业务侧覆写在 `static/css/app.css`，勿直接改本目录。

# FilePond（本地化静态资产）

- 用途：学生端「社会实践申报」四步向导 Step3 的五个上传区（templates/submissions/apply.html）
- 来源：pqina 官方仓库的 `dist/` 编译产物，逐个 raw 下载落盘（项目要求零 CDN 依赖，校园网/内网离线可用）
- 许可证：MIT（Copyright (c) PQINA / Rik Schennink）

## 版本与来源

| 文件 | 包 | 版本 | 源地址 |
| --- | --- | --- | --- |
| `filepond.min.js` / `filepond.min.css` | filepond | 4.32.12 | https://github.com/pqina/filepond → `dist/` |
| `locale/zh-cn.js` | filepond 语言包 | 4.32.12 | https://github.com/pqina/filepond → `locale/zh-cn.js` |
| `plugins/filepond-plugin-file-validate-type.min.js` | filepond-plugin-file-validate-type | 1.2.9 | https://github.com/pqina/filepond-plugin-file-validate-type → `dist/` |
| `plugins/filepond-plugin-file-validate-size.min.js` | filepond-plugin-file-validate-size | 2.2.8 | https://github.com/pqina/filepond-plugin-file-validate-size → `dist/` |
| `plugins/filepond-plugin-image-validate-size.min.js` | filepond-plugin-image-validate-size | 1.2.7 | https://github.com/pqina/filepond-plugin-image-validate-size → `dist/`（即需求里的 file-validate-image，用于图片格式/尺寸校验） |
| `plugins/filepond-plugin-image-preview.min.js` / `.min.css` | filepond-plugin-image-preview | 4.6.12 | https://github.com/pqina/filepond-plugin-image-preview → `dist/` |
| `plugins/filepond-plugin-image-exif-orientation.min.js` | filepond-plugin-image-exif-orientation | 1.0.11 | https://github.com/pqina/filepond-plugin-image-exif-orientation → `dist/` |
| `plugins/filepond-plugin-file-poster.min.js` / `.min.css` | filepond-plugin-file-poster | 2.5.2 | https://github.com/pqina/filepond-plugin-file-poster → `dist/` |
| `plugins/filepond-plugin-file-metadata.min.js` | filepond-plugin-file-metadata | 1.0.8 | https://github.com/pqina/filepond-plugin-file-metadata → `dist/` |

## 中文语言包的两个文件

- `locale/zh-cn.js`：上游原样搬运，是 **ESM**（`export default {...}`），供未来的打包器直接 import。
- `locale/zh-cn.global.js`：由上面的 ESM 生成的**浏览器全局版**（去掉 `export default`，包成 IIFE，
  赋值 `window.FilePondLocaleZhCN` 并调用 `FilePond.setOptions(...)`）。因为本项目的模板走 `<script src>`
  经典脚本、没有打包器，页面加载的是这个文件。**文案与上游逐字一致**，只改了导出形式。

重新生成（升级上游语言包后执行）：

```powershell
# 取 locale/zh-cn.js 里 export default 之后的对象字面量，套进 global 包装即可
$src = Get-Content static/vendor/filepond/locale/zh-cn.js -Raw
# 手工确认预览后再覆盖 locale/zh-cn.global.js
```

## 升级方式

替换同名文件即可；业务侧覆写在 `static/css/app.css`（`.filepond--*` 段）与 `static/js/apply-wizard.js`，
勿直接改本目录。
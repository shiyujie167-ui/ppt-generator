# 示例成品画廊

首页「示例同款风格」卡片与 `/viewer` 翻页预览用的静态资产,约 196MB,**不入 git**。

## 来源

裁剪自 MIT 许可的上游项目 [PPT Master](https://github.com/hugohe3/ppt-master)
(v4.2.0,作者 Hugo He)`examples/` 目录的全部 21 个示例——只拷了每个示例的
`svg_final/`(每页最终 SVG,图片已 base64 内嵌,无外部引用),`images/`、
`exports/`、`notes/` 等未带。许可证与声明见 `engine/LICENSE`、`engine/NOTICE.md`。

## 结构

```
examples.json          清单:id/title/styleName/style_id/description/tags/pages/cover/slides
<示例id>/*.svg         该示例的全部页面(与清单 slides[].file 对应)
```

`style_id` 是引擎视觉风格规范文档的 id(`engine/skills/ppt-master/references/
visual-styles/<style_id>.md`)。选中示例卡生成时,后端把它写进 style_brief,
让策略师锁定同款 visual_style 并读取对应规范。

## 目录缺失时

`app.py` 启动时检测 `examples.json`,缺失则自动降级:不挂载 `/examples`、
风格区只显示两张内置卡,其余功能不受影响。

## 重建方法

从旧仓库(或上游 clone)重拷:对 `ppt-master/examples/examples.json` 里
`projects` 的全部 21 条,把各自 `svg_final/*.svg` 拷到本目录 `<id>/` 下,并按
上面的结构生成 `examples.json`(styleName → style_id 的映射关系见 git 历史
中生成本目录的脚本,或直接沿用现有 examples.json 的对应字段)。

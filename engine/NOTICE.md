# NOTICE

本目录(`engine/`)内的生成引擎、规则文档与通用模板归档自开源项目
**PPT Master**(https://github.com/hugohe3/ppt-master ,v4.2.0),
作者 Hugo He,以 MIT 许可证分发。原始版权与许可声明见本目录下的
[LICENSE](LICENSE) 文件,依据该许可证保留。

本项目对上游内容做了裁剪与少量修改:

- 只保留 Web 生成流程用到的脚本闭包(9 个入口脚本及其依赖,约 4MB),
  移除了动画/音频/视频/AI 生图/交互编辑器等未使用的模块;
- 保留全部 workflows 与 references 规则文档(仅 Markdown,不含图片素材库);
- 移除了上游演示内容(examples/、中国电信与中汽研示例 deck);
- `templates/decks/mt_corporate_blue/` 为本公司自建模板,并非上游内容;
- `projects/` 下为本项目的任务工作产物,并非上游内容。

第三方声明:`skills/ppt-master/scripts/pptx_shapes/data/` 内含 Microsoft
Open XML SDK 的预置形状定义,其 Apache-2.0 / MIT 许可文本与 NOTICE
随目录一并保留,见该目录下的 LICENSE-* 与 NOTICE.md。

自本归档起,engine/ 独立演进,不再跟随上游同步。

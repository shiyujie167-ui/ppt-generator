# PPT Master Web

上传材料 → 选风格与模型 → 无人值守生成可编辑 PPTX。
项目直接通过 Responses API 驱动自己的受控 Agent,不依赖 Codex CLI、Claude CLI
或桌面会话。没有可用 API 配置时自动进入 **mock 演示模式**。

本项目**自包含**:生成引擎、规则文档与模板都在 `engine/` 内,不依赖任何外部
仓库。`engine/` 归档自 MIT 许可的开源项目
[PPT Master](https://github.com/hugohe3/ppt-master)(v4.2.0,作者 Hugo He)
并做了裁剪,详见 `engine/NOTICE.md` 与 `engine/LICENSE`;自归档起独立演进,
不再跟随上游。

## 启动

```bash
cd ~/Workplace/projects/ppt-web
.venv/bin/python migrate_v2.py   # 首次运行:建库(SQLite)+ 创建 admin 账号 + 导入历史数据
.venv/bin/python app.py
# 打开 http://127.0.0.1:8080 ,用 admin 账号登录
```

**账号与隔离**:所有页面与接口都要登录;材料库、任务与成品按账号隔离
(单账号在用、多账号就绪)。开放同事使用:
`.venv/bin/python migrate_v2.py adduser 用户名 密码`(`disable/enable 用户名` 停用/恢复，
`passwd 用户名 新密码` 改密并注销旧会话)。

首次安装依赖:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## 公司服务器部署

生产文件在 `ops/`。服务器沿用其他内部项目的 Git 规则：本机通过堡垒机
remote helper 把 `main` 推到 `/srv/git/ppt-web.git`，再显式部署该提交；push
本身不会自动上线。

```bash
git remote add company companyserver::/srv/git/ppt-web.git
git config remote.company.push refs/heads/main:refs/heads/main
git push company main
~/bin/company-ssh "sudo /opt/ppt-web/scripts/deploy.sh $(git rev-parse HEAD)"
```

部署采用每提交独立 release/venv、原子切换，并在切换前校验 Nginx 配置；
应用健康检查或 Nginx 重载失败时会自动回滚。运行时
`data/` 与 `engine/projects/` 外置持久化；SQLite 和材料/成品每天备份，保留
14 天。有生成任务正在运行时部署会拒绝重启，避免任务从头执行并重复计费。

首次服务器初始化运行 `sudo ops/bootstrap-server.sh`，API key 只写入
`/etc/ppt-web/ppt-web.env`。应用仅监听 `127.0.0.1:8080`，由 Nginx 发布在
`/ppt-generator/`；旧 `/ppt-web/` 地址会保留跳转兼容。

## API 配置

```bash
cp .env.example .env
```

至少配置:

```dotenv
PPT_API_BASE_URL=https://api.openai.com/v1
PPT_API_KEY=...
PPT_MODEL=gpt-5.6
PPT_WIRE_API=responses
```

接口必须支持 Responses API 的函数调用。程序也兼容旧变量
`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_API_KEY`,所以旧 `.env`
不必立刻改名。API Key 只放在服务进程的 HTTP Authorization 头里,不会传给
PPT Master 脚本或写入任务日志。

### 多模型对比

`.env` 里用 `PPT_MODEL2..9` 追加备选模型(各带自己的 `_BASE_URL`/`_API_KEY`/
`_WIRE`),前端表单和规划确认面板都可切换。`_WIRE=responses` 走 OpenAI
Responses API;`_WIRE=chat` 走 Chat Completions 兼容端点(如 DeepSeek 官方,
执行器内置双向协议适配与上下文截断)。同一份已确认大纲可以换不同模型分别
生成,任务卡显示各自的模型、耗时与 token 费用(费率配置 `_INPUT/OUTPUT_USD_PER_M`
后显示),便于横向对比。

## 目录

```
app.py             Web 服务(FastAPI):登录、材料库、任务 API、下载
db.py              SQLite 存储层:账号 / 登录会话 / 材料表(多账号就绪)
qa.py              用户材料库 + source_to_md 后台解析 + 材料问答(同步单轮)
jobs.py            任务模型 + 线程池队列 + SQLite 持久化(任务归属 user_id)
migrate_v2.py      首次建库迁移 + 账号管理命令(adduser/disable/enable/passwd)
runner.py          执行器入口:mock/真实模式分发 + 产物收集
runner_agent.py    Responses API 多轮工具循环 + 文件/脚本安全边界
native_company.py  公司模板原生合并:原稿封面/目录 + 生成正文页,纯标准库
prompts.py         风格选项 + 委托提示词模板
config.py          .env 加载与全局路径
templates/       前端页面(index.html 生成台 + login.html 登录页 + viewer.html 翻页预览器)
engine/          内置生成引擎(规则文档 + 脚本 + 模板;MIT 归档,见 engine/NOTICE.md)
engine/projects/ 每个任务的生成工作区(web_<任务ID>_*)
data/app.db      SQLite:users / sessions / materials / jobs(文件本体不入库)
data/library/    每账号一个材料库目录(原件 + 解析 .md),常驻可复用
data/company/    公司模板原稿 PPTX(原生封面/目录来源)+ cover_preview.png(风格卡真实封面图)
data/examples/   示例成品画廊(21 个预渲染示例;不入库,见其 README)
data/uploads/    每个任务的材料快照(硬链接自材料库,删库不影响历史任务)
data/outputs/    每个任务的最终 PPTX / 预览图
data/logs/       每个任务的运行日志
data/sample/     mock 模式使用的样例文件
```

## 说明

- **三栏交互(NotebookLM 式)**:左栏是账号的**材料库**(上传即入库并后台用
  引擎 source_to_md 解析成 Markdown,常驻、跨设备,可反复用于多次生成);
  中栏基于材料问答(同步单轮模型调用,不进任务队列,秒级返回,可多轮追问)
  与 PPT 内容配置;右栏模板挑选、模型选择与生成进度。提交生成时把勾选材料
  **硬链接快照**进任务目录(零空间开销),此后增删材料不影响已提交任务。
- **任务队列**:默认串行(一次只跑一个,费用最可控);`.env` 里 `JOB_WORKERS=2-4`
  可并行跑多个任务(适合同一材料多风格/多模型对比),API 并发与费用同时叠加。
- **超时**:单任务默认 90 分钟(`.env` 里 `JOB_TIMEOUT_MINUTES` 可调)。
- **费用**:如配置 `PPT_INPUT_USD_PER_M` / `PPT_OUTPUT_USD_PER_M`(以及缓存命中价
  `PPT_CACHED_USD_PER_M`,强烈建议配——缓存占长任务输入的 9 成以上,不配会按输入价虚高约 9 倍),
  任务卡显示估算费用(美元)。
- **规划确认(默认开启,人工把关)**:提交后先跑轻量规划任务(约 1 分钟)产出
  每页大纲,前端弹出确认面板——可直接编辑(改标题/要点、增删移页)、输入意见让
  AI 重排(可多轮)、切换风格(跟随模板卡或 AI 建议)与生成模型;确认后才派生
  正式生成任务(`mode=plan`、`/replan`、`/confirm-plan`),确认过的大纲以
  「最终页面计划」写入提示词、执行器逐页 1:1 落实。同一大纲可换模型分别生成对
  比。生成台可勾选「跳过确认直接生成」(`mode=direct`)。成品层面的「大面板单
  行填充」启发式检查仅记日志提醒,不再硬性拦截——质量判断交给规划确认与人工验收。
- **风格**:横向滑动条点选——公司蓝模板与公司蓝·自由版(真实封面预览)+ 21 个示例成品同款
  (带 `/viewer` 翻页预览;选中后以 custom 风格生成,style_brief 锁定该示例的
  visual_style 规范文档)。瑞士极简/深色商务/自定义卡已从 UI 移除,风格定义
  仍在 prompts.STYLES 供接口层兼容。生成阶段提示词声明「三阶段确认全权委托」,
  中途不会停下来等确认。
- **隔离**:每个真实任务只能写 `engine/projects/web_<任务ID>...`,不能修改模板库、
  skill 代码或其他项目。
- **工具边界**:Agent 可读取引擎/本任务上传目录,可安装模板到本任务项目,只能通过无
  shell 的参数数组运行 `engine/skills/ppt-master/scripts/` 下白名单内的 9 个脚本。
- **看图排版(视觉)**:驱动模型支持图片输入时(responses 协议默认开,chat 默认关,
  `PPT_MODEL*_VISION` 可覆盖),Agent 通过 `view_image` 工具逐张查看材料图片
  (大图自动缩样 ≤1568px,上下文只保留最近 2 张,全任务默认最多 12 次,
  `PPT_VIEW_IMAGE_MAX` 可调),确认主体与裁切后再安置到页面
  ——对应上游 image-searcher.md 的 Multimodal 分支;不支持视觉的模型自动走
  without-vision 降级分支(靠文件名 + 原文位置 + analyze_images 客观参数)。
- **长任务上下文**:默认启用 Responses server-side compaction(`PPT_COMPACT_THRESHOLD`),
  并使用稳定的 prompt cache key,减少长流程里的重复上下文和延迟。
- **API 重试**:5xx/429/断连/超时自动重试(`PPT_API_RETRIES`,默认 3 次),
  避免长任务因一次瞬时故障整单报废;4xx 配置类错误直接失败。
- **公司模板保真**:公司蓝任务导出后,`native_company.py` 以
  `COMPANY_TEMPLATE_SOURCE` 原稿为底自动合并——成品第 1、2 页是原稿的原生
  封面/目录页(只按 Agent 写的 `native_fill.json` 替换标题/日期/目录文字),
  第 3 页起并入生成的正文页并保留公司页眉页脚。勾选 AI 配图时,两种公司蓝
  风格都会额外生成 `images/company_cover_hero.png`,按原稿顶部约 3.404:1 的
  图片框自动居中 cover 裁切并填入;原图仍嵌在 PPTX 中,可在 PowerPoint 里继续
  调整裁剪。合并后执行母版保真度硬校验(原生版式完整、前两页非合成页、封面
  图片关系与裁切正确),不通过则任务判失败,不会下发走样成品。
- Web 无人值守运行不启动 SVG 实时预览 daemon,避免遗留进程占用端口。

## Public generation evidence

See [showcase/](showcase/) for sanitized server logs and case studies.

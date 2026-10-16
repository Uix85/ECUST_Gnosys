# reader 3

A lightweight, self-hosted EPUB reader that lets you read through EPUB books one chapter at a time. This makes it very easy to copy paste the contents of a chapter to an LLM, to read along. Basically - get epub books (e.g. [Project Gutenberg](https://www.gutenberg.org/) has many), open them up in this reader, copy paste text around to your favorite LLM, and read together and along.

This project was 90% vibe coded just to illustrate how one can very easily [read books together with LLMs](https://x.com/karpathy/status/1990577951671509438). I'm not going to support it in any way, it's provided here as is for other people's inspiration and I don't intend to improve it. Code is ephemeral now and libraries are over, ask your LLM to change it in whatever way you like.

## Usage

The project uses [uv](https://docs.astral.sh/uv/). So for example, download [Dracula EPUB3](https://www.gutenberg.org/ebooks/345) to this directory as `dracula.epub`, then:

```bash
uv run backend/reader/import_epub.py dracula.epub
```

This creates `books/dracula_data`, which registers the book to your local library. We can then run the server:

```bash
uv run server.py
```

And visit [localhost:8123](http://localhost:8123/) to see your current Library. You can easily add more books, or delete them from your library by deleting the folder. It's not supposed to be complicated or complex.

## License

MIT
## Project Layout

```text
server.py               项目根目录启动入口（应用和书库路由在 backend/api/server.py）
backend/server.py       兼容启动入口
backend/api/            FastAPI 应用装配与书库 HTTP 路由
backend/reader/         EPUB 导入、阅读服务、章节处理与四层笔记接口
backend/agent/          Agent 流程模块（runs.py、engine.py、tasks.py、tools.py、book.py、config.py）
backend/db/             SQLite 连接与 schema
frontend/templates/     Jinja 页面
frontend/static/        浏览器静态资源
scripts/                人手动运行的自检与批处理脚本
books/                  本地书库；每本书的数据仍在 <书名>_data/book.db
docs/                   规范、项目材料、章节草稿与原文节选
```
Run everything from the repo root:

```bash
uv run server.py                                            # 起服务
uv run scripts/check_semantic_para.py "<books 下的书目录名>"   # 语义层数据对账
uv run scripts/check_agent_endpoint.py                      # 端点自检
uv run scripts/run_batch.py -n 2                            # Agent 冒烟（2 段）
```

## 段落语义 Agent

阅读页可以为单段生成解释，也可以按章节批量生成。首轮生成后会由新上下文独立复核；通过的解释自动出现在正文旁，未通过的候选稿保存在“待核稿”中，须由读者采纳。任务记录保存在每本书的 `book.db`。

首次配置时，将 `.env.example` 复制为 `.env`，将 `.env.secrets.example` 复制为 `.env.secrets`，并在后者填写 `AGENT_API_KEY`。`.env.secrets` 已加入忽略规则。模型接口使用兼容 Chat Completions 工具调用的服务；默认模型为 `gpt-4.1-mini`。端点、模型、请求超时和每段执行预算可在 `.env` 中调整。

默认每本书最多同时处理两个段落；每段最多 12 次模型请求、20 次工具调用和 5 分钟。启动后可在阅读页查看进度、取消、重试失败段落和审阅待核稿。未结束任务会在服务重启后标记为中断。

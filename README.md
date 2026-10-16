# Dttack_reader

这是一个围绕 AI 辅助阅读器整理的项目工作区，包含当前阅读器、参考项目、项目文书和历史代码。日常运行的应用位于 `ECUST_Gnosys/`；根目录本身不提供统一的启动命令。

## 当前应用：ECUST Gnosys

`ECUST_Gnosys/` 是当前的本地 EPUB 阅读器项目，提供章节阅读、阅读笔记和段落级 AI 解释。

```bash
cd ECUST_Gnosys
uv sync
uv run server.py
```

启动后访问 <http://127.0.0.1:8123/>。安装、模型配置、书籍导入和 Agent 结构说明见 [ECUST_Gnosys/README.md](ECUST_Gnosys/README.md)。

## 工作区目录

```text
Dttack_reader/
├── ECUST_Gnosys/          当前阅读器应用
├── 参考项目/
│   └── Misaka-Agent/      Agent 架构参考代码
├── 文书材料汇总/          产品需求、提示词、系统架构和项目文书索引
└── 旧文件/                早期阅读器实现与网页原型
```

### `ECUST_Gnosys/`

当前应用的代码、前端页面、运行配置和文档。书籍数据在该项目自己的 `books/` 目录中。项目的详细使用说明和代码结构以其 README 为准。

### `参考项目/Misaka-Agent/`

用于阅读和比较 Agent 设计的参考项目。它有独立的安装方式和运行要求，不是 ECUST Gnosys 的启动入口。

### `文书材料汇总/`

按主题整理产品需求、提示词与工作流、研究设计、系统架构、项目背景、访谈记录和原型材料。查看[文书索引](文书材料汇总/README.md)可找到材料说明和来源链接。

### `旧文件/`

保留早期 `reader` 实现、历史维护材料和网页原型，供查阅旧方案与项目演进。当前应用开发和启动请使用 `ECUST_Gnosys/`。

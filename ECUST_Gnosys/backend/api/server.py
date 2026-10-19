"""
ECUST_Reader3 —— FastAPI 阅读服务器（应用入口与书库路由）。

阅读器、Agent、数据库模块分别归入 reader/、agent/ 和 db/。
"""

import os
import shutil
import tempfile
import urllib.parse

from fastapi import FastAPI, Request, HTTPException, File, UploadFile, Body
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from reader.models import Book
from reader.import_epub import process_epub_to_sqlite
from reader.book_service import (
    BOOKS_DIR,
    load_book_cached,
    clear_book_caches,
    _get_heading_toc,
    _get_toc_pages,
    _build_chapters,
    _get_unified_html_cached,
    _chapter_slice_anchors,
    _slice_content,
    _get_anchor_chapter_map,
)
from reader.auxiliary_reading import router as layer_router
from api.agent_bridge import router as agent_bridge_router
from agent.api import agent_router, chat_router
from agent.runs.records import allow_book_runs, recover_interrupted_runs
from agent.runs.loop import cancel_book_runs
from db.schema import close_db
from starlette.middleware.gzip import GZipMiddleware
from api.versioned import include_legacy_router, include_versioned_router

app = FastAPI()
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # 应用根目录
templates = Jinja2Templates(directory=os.path.join(ROOT_DIR, "frontend", "templates"))
templates.env.auto_reload = True  # reload templates on each request during dev
STATIC_DIR = os.path.join(ROOT_DIR, "frontend", "static")


def _asset_version():
    """静态资源内容一改，查询串就变，避免浏览器继续用旧 JS/CSS。"""
    paths = [
        os.path.join(STATIC_DIR, "css", "workspace.css"),
        os.path.join(STATIC_DIR, "js", "workspace.js"),
        os.path.join(STATIC_DIR, "js", "markdown_render.js"),
    ]
    return str(max((os.stat(p).st_mtime_ns for p in paths if os.path.exists(p)), default=0))


templates.env.globals["asset_version"] = _asset_version
app.mount("/static", StaticFiles(directory=os.path.join(ROOT_DIR, "frontend", "static")), name="static")


@app.exception_handler(HTTPException)
async def api_http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/v1/"):
        detail = exc.detail
        if isinstance(detail, dict):
            details = detail.get("details")
            if details is None:
                details = {key: value for key, value in detail.items()
                           if key not in ("code", "message")}
                details = details or None
            error = {"code": detail.get("code", f"http_{exc.status_code}"),
                     "message": detail.get("message", str(detail)), "details": details}
        else:
            error = {"code": f"http_{exc.status_code}", "message": str(detail), "details": None}
        return JSONResponse(status_code=exc.status_code, content={"error": error}, headers=exc.headers)
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def api_validation_error(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/api/v1/"):
        return JSONResponse(status_code=422, content={"error": {
            "code": "invalid_request", "message": "请求格式无效",
            "details": jsonable_encoder(exc.errors())}})
    return await request_validation_exception_handler(request, exc)

from reader.workspace import router as workspace_router
for api_router in (layer_router, agent_router, workspace_router, chat_router):
    include_versioned_router(app, api_router)
    include_legacy_router(app, api_router)
app.include_router(agent_bridge_router)


@app.on_event("startup")
async def recover_agent_tasks():
    recover_interrupted_runs()
    from agent.chat import recover
    from agent.tools.book import book_db
    from reader.workspace import dumps, now
    import json
    import uuid
    for folder in os.listdir(BOOKS_DIR) if os.path.isdir(BOOKS_DIR) else []:
        if not folder.endswith('_data') or not os.path.isfile(os.path.join(BOOKS_DIR, folder, 'book.db')):
            continue
        recover(folder)
        db = book_db(folder)
        for row in db.execute("SELECT r.id,r.request_json,x.* FROM agent_runs r JOIN layer_results x ON r.id=x.run_id WHERE r.status='interrupted' AND x.candidate_json IS NOT NULL").fetchall():
            if not db.execute('SELECT 1 FROM layer_drafts WHERE run_id=?', (row['id'],)).fetchone():
                request = json.loads(row['request_json'])
                with db:
                    db.execute('INSERT INTO layer_drafts VALUES(?,?,?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, row['id'], request['note_key'], request['layer'], dumps(request['target']), row['candidate_json'], dumps({'approved': False, 'issues': ['服务重启，候选稿未完成复核']}), row['baseline_json'], 'pending', now()))

# 大 JSON / 长章节 HTML 压缩传输（§5-6 章正文与语义层条目可达 1MB+）
# compresslevel=1：压缩比仍约 3-4 倍，但耗时远低于默认 6（本地与局域网都不吃亏）
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=1)

# ═══════════════════════════════════════════
# HTTP 路由
# ═══════════════════════════════════════════

# 书库置顶书籍（按目录 id 匹配，顺序即显示顺序；其余书籍保持原顺序）
PINNED_BOOK_IDS = [
    "精神现象学 黑格尔着 (精神现象学 黑格尔着.txt) (z-library.sk, 1lib.sk, z-lib.sk)_data",
]

# ── 书库首页 GET / ──
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def library_view(request: Request):
    """书库页面只返回静态壳；书目由版本化 API 提供。"""
    return templates.TemplateResponse(request, "library.html", {})


# ── 设置页：读写 database/settings.db ──
@app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_view(request: Request):
    return templates.TemplateResponse(request, "settings.html", {})


@app.get("/api/settings", include_in_schema=False)
async def read_settings():
    from agent.runtime.config import settings_snapshot
    return settings_snapshot()


@app.put("/api/settings", include_in_schema=False)
async def update_settings(body: dict = Body(...)):
    from agent.runtime.config import save_settings
    try:
        # 不缓存：保存后下一次 load_config() 立刻从数据库读取
        return save_settings(body.get("values", {}))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None

# ── 上传处理 POST /upload ──
@app.post("/upload", include_in_schema=False)
async def upload_epub(file: UploadFile = File(...)):
    """【上传处理】接收 EPUB/TXT 文件，处理后加入书库"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="未指定文件名")

    ext = os.path.splitext(file.filename)[1].lower()

    if ext == '.epub':
        suffix = '.epub'
        file_type = "EPUB"
    elif ext == '.txt':
        suffix = '.txt'
        file_type = "TXT"
    else:
        raise HTTPException(status_code=400, detail="只支持 EPUB 和 TXT 格式的文件")

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        from reader.import_document import import_document
        from starlette.concurrency import run_in_threadpool
        out_dir = await run_in_threadpool(import_document, tmp_path, BOOKS_DIR, file.filename)

        # Get book info from the new DB
        folder = os.path.basename(out_dir)
        # 先清旧缓存（同名重复上传时避免残留指向已删除的旧 DB），再加载新书，
        # 让新书缓存立即生效：用户首次打开这本书直接命中，无需重新构建。
        clear_book_caches(folder)
        book = load_book_cached(folder)

        return JSONResponse({
            "success": True,
            "id": folder,
            "title": book.metadata.title if book else folder,
            "author": ", ".join(book.metadata.authors) if book else "",
            "folder": folder,
            "chapters": len(book.spine) if book else 0,
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"处理 {file_type} 失败: {str(e)}")
    finally:
        os.unlink(tmp_path)


# ── 删书 DELETE /api/books/{book_id} ──
@app.delete("/api/books/{book_id}", include_in_schema=False)
async def delete_book(book_id: str):
    """【删书】关闭该书数据库连接后整目录删除。
    schema.get_db 的常驻连接会锁住 book.db/-wal/-shm，Windows 上不先 close 则目录删不干净。"""
    safe = os.path.basename(book_id)
    if not safe.endswith("_data"):
        raise HTTPException(status_code=400, detail="非法的书目录")
    out_dir = os.path.join(BOOKS_DIR, safe)
    if not os.path.isdir(out_dir):
        raise HTTPException(status_code=404, detail="书不存在")
    if safe in PINNED_BOOK_IDS:
        raise HTTPException(status_code=400, detail="置顶书不可删除")

    await cancel_book_runs(safe)
    from agent.chat import ACTIVE as CHAT_ACTIVE, SIGNALS
    for key, signal in list(SIGNALS.items()):
        if key[0] == safe:
            signal.set()
    pending = [task for key, task in list(CHAT_ACTIVE.items()) if key[0] == safe]
    if pending:
        import asyncio
        await asyncio.gather(*pending, return_exceptions=True)
    try:
        clear_book_caches(safe)
        close_db(os.path.join(out_dir, "book.db"))   # 先释放句柄，否则 rmtree 删不掉 db 三件套
        shutil.rmtree(out_dir)
    finally:
        allow_book_runs(safe)
    return {"ok": True, "deleted": safe}

# ── 首页跳转 GET /read/{book_id} → 第 0 章 ──
@app.get("/read/{book_id}", response_class=HTMLResponse, include_in_schema=False)
async def redirect_to_first_chapter(book_id: str):
    """【首页跳转】重定向到 /read/{book_id}/0。
    必须真重定向（而非直接渲染）：正文图片 src 是相对路径（images/...），
    若 URL 停在 /read/{book_id} 会解析成 /read/images/... 导致图片 404。"""
    from reader.workspace import get_state
    saved = await get_state(book_id)
    ci, pi = saved.get('chapter_idx', 0), saved.get('paragraph_index', 0)
    return RedirectResponse(url=f"/read/{urllib.parse.quote(book_id)}/{ci}#source={ci}:{pi}", status_code=302)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """【图标占位】避免浏览器自动请求在控制台刷 404"""
    return Response(status_code=204)

# ── 章节切片共用（页面路由与 API 共用，避免重复逻辑）──
def _get_chapter_slice(book_id: str, book: Book, chapter_idx: int):
    """【章节切片】返回 (chapters, ch, html)；章越界时抛 404"""
    toc_pages = _get_toc_pages(book_id, book)
    chapters = _build_chapters(toc_pages)
    if chapter_idx < 0 or chapter_idx >= len(chapters):
        raise HTTPException(status_code=404, detail="Chapter not found")
    ch = chapters[chapter_idx]
    unified = _get_unified_html_cached(book_id, book)
    start_anchor, end_anchor = _chapter_slice_anchors(chapters, toc_pages, book, book_id, chapter_idx)
    html = _slice_content(unified, start_anchor, end_anchor)
    return chapters, ch, html

# ── 阅读页面 GET /read/{book_id}/{chapter_idx} ──
@app.get("/read/{book_id}/{chapter_idx}", response_class=HTMLResponse, include_in_schema=False)
async def read_chapter(request: Request, book_id: str, chapter_idx: int):
    """返回阅读页面静态壳；目录与正文由版本化 API 提供。"""
    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    chapters = _build_chapters(_get_toc_pages(book_id, book))
    if chapter_idx < 0 or chapter_idx >= len(chapters):
        raise HTTPException(status_code=404, detail="Chapter not found")
    return templates.TemplateResponse(request, "reader.html", {
        "book_id": book_id,
        "chapter_idx": chapter_idx,
        "note_mode": False,
    })

# ── 笔记界面 GET /notes/{book_id} ──
@app.get("/notes/{book_id}", response_class=HTMLResponse, include_in_schema=False)
async def notes_page(request: Request, book_id: str):
    """【笔记界面】全书层（L1）+ 章节层（L2）专门页面；四角星入口跳转至此"""
    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    return templates.TemplateResponse(request, "reader.html", {
        "book_id": book_id, "chapter_idx": 0, "note_mode": True,
    })

# ── 整章 AJAX API GET /api/full_chapter/{book_id}/{chapter_idx} ──
@app.get("/api/full_chapter/{book_id}/{chapter_idx}", include_in_schema=False)
async def api_full_chapter(book_id: str, chapter_idx: int):
    """【整章API】返回一整章的 HTML + 节列表"""
    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    chapters, ch, html = _get_chapter_slice(book_id, book, chapter_idx)

    return JSONResponse({
        "chapter_idx": chapter_idx,
        "title": ch['title'],
        "html": html,
        "total_chapters": len(chapters),
    })

# ── 图片服务 GET /read/{book_id}/images/{image_name} ──
@app.get("/read/{book_id}/images/{image_name}", include_in_schema=False)
async def serve_image(book_id: str, image_name: str):
    """【图片服务】返回书籍内的图片文件"""
    safe_book_id = os.path.basename(book_id)
    safe_image_name = os.path.basename(image_name)
    img_path = os.path.join(BOOKS_DIR, safe_book_id, "images", safe_image_name)
    if not os.path.exists(img_path):
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(img_path)


@app.get("/api/v1/books")
async def list_books_api():
    books = []
    if os.path.exists(BOOKS_DIR):
        for item in os.listdir(BOOKS_DIR):
            if item.endswith("_data") and os.path.isdir(os.path.join(BOOKS_DIR, item)):
                book = load_book_cached(item)
                if book:
                    books.append({"id": item, "title": book.metadata.title,
                                  "author": ", ".join(book.metadata.authors)})
    books.sort(key=lambda b: (0, PINNED_BOOK_IDS.index(b["id"])) if b["id"] in PINNED_BOOK_IDS else (1, 0))
    return {"items": books}


@app.get("/api/v1/books/{book_id}")
async def get_book_api(book_id: str):
    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    chapters = _build_chapters(_get_toc_pages(book_id, book))
    return {"id": book_id, "title": book.metadata.title,
            "author": ", ".join(book.metadata.authors), "chapter_count": len(chapters),
            "anchor_map": _get_anchor_chapter_map(book_id, book)}


@app.post("/api/v1/books/import")
async def import_book_api(file: UploadFile = File(...)):
    return await upload_epub(file)


@app.delete("/api/v1/books/{book_id}")
async def delete_book_api(book_id: str):
    return await delete_book(book_id)


@app.get("/api/v1/books/{book_id}/chapters/{chapter_idx}/html")
async def chapter_html_api(book_id: str, chapter_idx: int):
    return await api_full_chapter(book_id, chapter_idx)


@app.get("/api/v1/books/{book_id}/images/{image_name}")
async def book_image_api(book_id: str, image_name: str):
    return await serve_image(book_id, image_name)


@app.get("/api/v1/settings")
async def get_settings_api():
    return await read_settings()


@app.put("/api/v1/settings")
async def put_settings_api(body: dict = Body(...)):
    return await update_settings(body)


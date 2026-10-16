"""
ECUST_Reader3 —— FastAPI 阅读服务器（应用入口与书库路由）。

阅读器、Agent、数据库模块分别归入 reader/、agent/ 和 db/。
"""

import os
import shutil
import tempfile
import urllib.parse

from fastapi import FastAPI, Request, HTTPException, File, UploadFile
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
from agent.run_api import router as agent_router
from agent.run_service import allow_book_runs, recover_interrupted_runs
from agent.runs import cancel_book_runs
from db.schema import close_db
from starlette.middleware.gzip import GZipMiddleware

app = FastAPI()
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # 应用根目录
templates = Jinja2Templates(directory=os.path.join(ROOT_DIR, "frontend", "templates"))
templates.env.auto_reload = True  # reload templates on each request during dev
app.mount("/static", StaticFiles(directory=os.path.join(ROOT_DIR, "frontend", "static")), name="static")

# 辅助阅读（四层级 AI 功能）独立挂载，与基础阅读路由隔离
app.include_router(layer_router)
app.include_router(agent_router)


@app.on_event("startup")
async def recover_agent_tasks():
    recover_interrupted_runs()

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
@app.get("/", response_class=HTMLResponse)
async def library_view(request: Request):
    """【书库页面】列出所有已处理书籍的首页"""
    books = []

    if os.path.exists(BOOKS_DIR):
        for item in os.listdir(BOOKS_DIR):
            if item.endswith("_data") and os.path.isdir(os.path.join(BOOKS_DIR, item)):
                book = load_book_cached(item)
                if book:
                    books.append({
                        "id": item,
                        "title": book.metadata.title,
                        "author": ", ".join(book.metadata.authors),
                    })

    # 置顶书籍排最前（按 PINNED_BOOK_IDS 顺序），其余保持原顺序（稳定排序）
    books.sort(key=lambda b: (0, PINNED_BOOK_IDS.index(b["id"])) if b["id"] in PINNED_BOOK_IDS else (1, 0))

    return templates.TemplateResponse(request, "library.html", {"books": books})

# ── 上传处理 POST /upload ──
@app.post("/upload")
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
        if ext == '.epub':
            out_dir = process_epub_to_sqlite(tmp_path, BOOKS_DIR)
        elif ext == '.txt':
            raise HTTPException(status_code=400, detail="TXT 支持尚未迁移到 SQLite")

        # Get book info from the new DB
        folder = os.path.basename(out_dir)
        # 先清旧缓存（同名重复上传时避免残留指向已删除的旧 DB），再加载新书，
        # 让新书缓存立即生效：用户首次打开这本书直接命中，无需重新构建。
        clear_book_caches(folder)
        book = load_book_cached(folder)

        return JSONResponse({
            "success": True,
            "title": book.metadata.title if book else folder,
            "author": ", ".join(book.metadata.authors) if book else "",
            "folder": folder,
            "chapters": len(book.spine) if book else 0,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"处理 {file_type} 失败: {str(e)}")
    finally:
        os.unlink(tmp_path)


# ── 删书 DELETE /api/books/{book_id} ──
@app.delete("/api/books/{book_id}")
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
    try:
        clear_book_caches(safe)
        close_db(os.path.join(out_dir, "book.db"))   # 先释放句柄，否则 rmtree 删不掉 db 三件套
        shutil.rmtree(out_dir)
    finally:
        allow_book_runs(safe)
    return {"ok": True, "deleted": safe}

# ── 首页跳转 GET /read/{book_id} → 第 0 章 ──
@app.get("/read/{book_id}", response_class=HTMLResponse)
async def redirect_to_first_chapter(book_id: str):
    """【首页跳转】重定向到 /read/{book_id}/0。
    必须真重定向（而非直接渲染）：正文图片 src 是相对路径（images/...），
    若 URL 停在 /read/{book_id} 会解析成 /read/images/... 导致图片 404。"""
    return RedirectResponse(url=f"/read/{urllib.parse.quote(book_id)}/0", status_code=302)


@app.get("/favicon.ico")
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
@app.get("/read/{book_id}/{chapter_idx}", response_class=HTMLResponse)
async def read_chapter(request: Request, book_id: str, chapter_idx: int):
    """【阅读页面】chapter_idx = 章索引（0-based），每章为连续滚动页"""
    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    heading_toc = _get_heading_toc(book_id, book)
    chapters, ch, chapter_content = _get_chapter_slice(book_id, book, chapter_idx)

    return templates.TemplateResponse(request, "reader.html", {
        "book": book,
        "book_id": book_id,
        "heading_toc": heading_toc,
        "chapter_idx": chapter_idx,
        "chapter_title": ch['title'],
        "chapter_content": chapter_content,
        "total_chapters": len(chapters),
        "anchor_map": _get_anchor_chapter_map(book_id, book),
    })

# ── 笔记界面 GET /notes/{book_id} ──
@app.get("/notes/{book_id}", response_class=HTMLResponse)
async def notes_page(request: Request, book_id: str):
    """【笔记界面】全书层（L1）+ 章节层（L2）专门页面；四角星入口跳转至此"""
    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    toc_pages = _get_toc_pages(book_id, book)
    chapters = _build_chapters(toc_pages)
    # 每逻辑章关联其起始页所在的 spine 文件（spine_order）作为章节层笔记键：
    # 标题树逻辑章（阅读页 chapter_idx）与 spine 文件顺序可能不一致
    # （TXT 转换的 EPUB 常把一章拆成多个碎片文件），读写都走同一映射保证一致。
    chapter_list = []
    for i, ch in enumerate(chapters):
        sp = None
        if 0 <= ch['start_page'] < len(toc_pages):
            sp = toc_pages[ch['start_page']].get('spine_order')
        chapter_list.append({"idx": i, "title": ch['title'], "spine_order": sp})

    return templates.TemplateResponse(request, "notes.html", {
        "book_id": book_id,
        "book_title": book.metadata.title,
        "chapters": chapter_list,
    })

# ── 整章 AJAX API GET /api/full_chapter/{book_id}/{chapter_idx} ──
@app.get("/api/full_chapter/{book_id}/{chapter_idx}")
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
@app.get("/read/{book_id}/images/{image_name}")
async def serve_image(book_id: str, image_name: str):
    """【图片服务】返回书籍内的图片文件"""
    safe_book_id = os.path.basename(book_id)
    safe_image_name = os.path.basename(image_name)
    img_path = os.path.join(BOOKS_DIR, safe_book_id, "images", safe_image_name)
    if not os.path.exists(img_path):
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(img_path)


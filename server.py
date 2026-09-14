"""
PDFScan 离线切分 HTTP 服务
=========================
把 PDFScan 的切分能力以本地 HTTP 接口暴露给其它应用，全程离线
（OCR 用本地 Tesseract，不联网；PyMuPDF 亦本地）。

启动（首次需联网装依赖）:
    pip install fastapi uvicorn python-multipart
    python server.py                      # 监听 http://127.0.0.1:8000
或:
    uvicorn server:app --host 127.0.0.1 --port 8000

接口:
    GET  /health                    健康检查 + 显示 Tesseract 路径
    POST /split                     {"pdf_path": "路径", "out_dir": "可选输出根"}
    POST /split/upload              multipart: file=PDF, out_dir=可选
    GET  /download?path=绝对路径    下载某个切分产物（防目录穿越）

说明:
    - 仅监听 127.0.0.1，不对外开放；如需局域网，请自行用反向代理并加鉴权。
    - 大册 OCR 耗时较长，调用方请设置足够超时（如 300s）。
    - 产物结构: out_dir/<原文件名>/<标题>.pdf + manifest.csv
    - manifest 中每条含 out(绝对路径)，可直接用 /download 取回。
"""
import os
import shutil
import tempfile
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

import multiprocessing as mp
# Windows 下线程内 spawn 子进程更稳（避免递归 import 主模块）
try:
    mp.set_start_method("spawn", force=True)
except RuntimeError:
    pass

from segment_redhead import segment, _find_tesseract

app = FastAPI(title="PDFScan 离线切分服务", version="1.0")

# 记录所有允许下载的输出根（用于 /download 防目录穿越）
_ALLOWED_ROOTS = set()


class SplitRequest(BaseModel):
    pdf_path: str
    out_dir: str | None = None


@app.get("/health")
def health():
    return {"status": "ok", "offline": True, "tesseract": _find_tesseract()}


@app.post("/split")
async def split(req: SplitRequest):
    if not os.path.isfile(req.pdf_path):
        raise HTTPException(status_code=400, detail=f"pdf_path 不存在: {req.pdf_path}")
    out_dir = req.out_dir or os.path.dirname(os.path.abspath(req.pdf_path))
    _ALLOWED_ROOTS.add(os.path.abspath(out_dir))
    try:
        manifest = await run_in_threadpool(segment, req.pdf_path, out_dir)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"切分失败: {e}")
    return {"pdf": req.pdf_path, "out_dir": out_dir, "count": len(manifest), "manifest": manifest}


@app.post("/split/upload")
async def split_upload(file: UploadFile = File(...), out_dir: str | None = None):
    tmp = tempfile.mkdtemp(prefix="pdfscan_")
    name = os.path.basename(file.filename or "upload.pdf")
    path = os.path.join(tmp, name)
    with open(path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    if out_dir:
        out_dir = os.path.abspath(out_dir)
    else:
        out_dir = tmp  # 产物与上传同目录，便于调用方下载
    _ALLOWED_ROOTS.add(os.path.abspath(out_dir))
    _ALLOWED_ROOTS.add(os.path.abspath(tmp))
    try:
        manifest = await run_in_threadpool(segment, path, out_dir)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"切分失败: {e}")
    finally:
        # 指定了外部 out_dir 时，清理上传临时文件（产物已落盘到 out_dir）
        if out_dir != tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return {"pdf": path, "out_dir": out_dir, "count": len(manifest), "manifest": manifest}


@app.get("/download")
def download(path: str = Query(...)):
    ap = os.path.abspath(path)
    if not os.path.isfile(ap):
        raise HTTPException(status_code=404, detail="文件不存在")
    if not any(os.path.commonpath([ap, root]) == root for root in _ALLOWED_ROOTS):
        raise HTTPException(status_code=403, detail="禁止访问该路径")
    return FileResponse(ap, filename=os.path.basename(ap))

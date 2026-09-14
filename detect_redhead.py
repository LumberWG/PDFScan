import os, fitz, numpy as np


def detect_redhead_covers(pdf_path, zoom=0.5):
    """检测红头文件封面页：顶部有公司名大字号行 + 其下方有居中大字号标题行。
    返回 [(page_no, title_bbox), ...]，title_bbox=(y0,y1,x0,x1) 为 zoom 坐标。"""
    doc = fitz.open(pdf_path)
    covers = []
    for p in range(doc.page_count):
        page = doc[p]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        h, w = pix.height, pix.width
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(h, w, -1)
        gray = arr[:, :, :3].mean(axis=2)
        R = arr[:, :, 0].astype(int)
        G = arr[:, :, 1].astype(int)
        B = arr[:, :, 2].astype(int)
        ink = (gray < 205) | ((R > 140) & (G < 110) & (B < 110))
        rowcnt = ink.sum(axis=1)

        def col_center(rr):
            cols = np.where(ink[rr])[0]
            return int((cols.min() + cols.max()) / 2)

        def _record(a, r):
            height = r - a
            if height < 16:
                return
            width = int(rowcnt[a:r].max())
            cy = (a + r) // 2
            cx = col_center(a)
            cols0 = np.where(ink[a])[0]
            x0, x1 = int(cols0.min()), int(cols0.max())
            centered = abs(cx - w / 2) <= 0.15 * w
            if 0.12 * w <= width <= 0.85 * w and height <= 0.40 * h and centered:
                runs.append((height, cy, cx, a, r, x0, x1))

        # 收集所有“大字号居中”文本行
        runs = []  # (height, cy, cx, a, r, x0, x1)
        in_run = False
        a = 0
        for r in range(h):
            if rowcnt[r] > 0:
                if not in_run:
                    in_run = True
                    a = r
            else:
                if in_run:
                    _record(a, r)
                    in_run = False
        if in_run:
            _record(a, h)

        if not runs:
            continue
        # 公司名：顶部(前28%)的大行
        comp = [u for u in runs if u[1] < 0.28 * h]
        if not comp:
            continue
        comp_cy = min(u[1] for u in comp)
        # 标题：位于公司名下方、上半页(前62%)的大行
        titles = [u for u in runs if u[1] > comp_cy + 8 and u[1] < 0.62 * h]
        if not titles:
            continue
        # 取公司名下方字号最大的大行作为标题（文档标题通常最大）
        titles.sort(key=lambda u: -u[0])
        best = titles[0]
        _, y0, y1, x0, x1 = best[3], best[3], best[4], best[5], best[6]
        covers.append((p + 1, (max(0, y0 - 3), min(h, y1 + 3), max(0, x0 - 6), min(w, x1 + 6))))
    doc.close()
    return covers


def ocr_title_crop(pdf_path, page_no, bbox, zoom=2.0, lang="chi_sim"):
    """裁剪封面标题区域并 OCR，返回标题文本。"""
    import os, shutil, pytesseract
    from PIL import Image
    # 优先用项目内 vendor 便携版（免安装分发），否则回退系统 PATH
    vend = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "vendor", "tesseract", "tesseract.exe")
    if os.path.isfile(vend):
        tess = vend
    else:
        tess = shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    pytesseract.pytesseract.tesseract_cmd = tess
    vdata = os.path.join(os.path.dirname(tess), "tessdata")
    if os.path.isdir(vdata):
        os.environ["TESSDATA_PREFIX"] = vdata
    doc = fitz.open(pdf_path)
    pix = doc[page_no - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    y0, y1, x0, x1 = bbox
    sy0, sy1 = int(y0 * zoom / 0.5), int(y1 * zoom / 0.5)
    sx0, sx1 = int(x0 * zoom / 0.5), int(x1 * zoom / 0.5)
    crop = img.crop((sx0, sy0, sx1, sy1))
    doc.close()
    txt = pytesseract.image_to_string(crop, lang=lang)
    return txt.strip()


if __name__ == "__main__":
    import glob
    for pdf in sorted(glob.glob(r"D:\Backup\RayChan\*.pdf")):
        if pdf.endswith(".ocr.json"):
            continue
        cands = detect_redhead_covers(pdf)
        if cands:
            print(os.path.basename(pdf), "->", [c[0] for c in cands])

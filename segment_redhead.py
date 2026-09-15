"""红头文档级切分：基于OCR文本，定位每个红头独立文档(例会/纪要/通知/报告…)的起始页，
按边界切片并以其标题命名。文件名仅作为装订册名(输出文件夹)。
用法: python segment_redhead.py <pdf> [--out DIR]
"""
import sys, os, re, json, csv, argparse, shutil
import pymupdf as fitz
import multiprocessing as mp
import pytesseract
from PIL import Image
from pytesseract import Output

def _find_tesseract():
    """优先用项目内 vendor 便携版（免安装分发），否则回退系统 PATH / 常见安装位置。"""
    here = os.path.dirname(os.path.abspath(__file__))
    vend = os.path.join(here, "vendor", "tesseract", "tesseract.exe")
    if os.path.isfile(vend):
        return vend
    p = shutil.which("tesseract")
    if p:
        return p
    for cand in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                 r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if os.path.isfile(cand):
            return cand
    return r"C:\Program Files\Tesseract-OCR\tesseract.exe"

pytesseract.pytesseract.tesseract_cmd = _find_tesseract()
# 若便携版自带 tessdata，则指向它，避免依赖系统语言包
_vend_data = os.path.join(os.path.dirname(pytesseract.pytesseract.tesseract_cmd), "tessdata")
if os.path.isdir(_vend_data):
    os.environ["TESSDATA_PREFIX"] = _vend_data
TESS_LANG = "chi_sim"
OCR_ZOOM = 1.6              # OCR 缩放：横向分辨率不降，保证中文识别率
# ---- 取图范围（提速核心：只 OCR 真正需要的区域）----
TOP_FAST_RATIO = 0.22       # 首选窄带：红头公司名与文档标题基本都在页顶 22% 内
FOOT_RATIO = 0.08           # 页脚条带：页码「第X页共Y页」在页脚（旧版 50% 条带把它裁掉了）
TOP_RATIO = 0.5             # 回退宽条带：仅当窄带读不出有效文字时才用
BAND_GAP = 24               # 窄带与页脚拼接时的白边像素，避免两块文字粘连成一行
OCR_CONFIG_FAST = "--psm 6" # 单块文本模式：跳过版面分析，更快且对标题行更准
TEXT_LAYER_MIN_CN = 10      # PDF 内嵌文本层中文数达到该值即直接采用（免 OCR，秒出）
BLANK_ZOOM = 0.25           # 空白页快筛的渲染缩放
BLANK_DARK_RATIO = 0.002    # 暗像素占比低于此 -> 空白页，直接跳过 OCR
SPARSE_DARK_RATIO = 0.01    # 暗像素占比低于此 -> 整页极稀疏（封面候选）
COVER_MAX_PAGES = 2         # 允许作为封面被前向合并的最大页数（防止误吞真实文档）
OCR_CACHE_SUFFIX = ".ocr.json"
OSD_CACHE_SUFFIX = ".osd.json"
# OCR 后端：默认 "rapidocr"（离线 ONNX 识别，需 x86-64 Python 才能装 rapidocr-onnxruntime）；
# 设为 "tesseract" 可强制走 Tesseract。RapidOCR 未安装时自动回退 Tesseract 并告警一次。
OCR_BACKEND = os.environ.get("PDFSCAN_OCR_BACKEND", "rapidocr").lower()


class SegmentCancelled(Exception):
    """切分被用户取消（GUI 停止按钮）。已完成页的 OCR 缓存已落盘，可续跑。"""

DOC_KW = ["例会", "纪要", "通知", "通报", "报告", "申请", "决定", "决议",
          "方案", "计划", "制度", "规定", "办法", "细则", "意见", "函",
          "批复", "总结", "公告", "声明", "讲话", "责任书", "承诺书",
          "条例", "规程", "规范", "手册"]
# 表单类：要求行尾命中（如「…登记表」「…检查表」「…责任书」），降低正文误报
FORM_KW = ["表", "单", "书", "名册", "台账", "清单", "花名册", "记录表", "统计表", "汇总表"]
EXCLUDE_PREFIX = ("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.", "0.",
                  "附件", "附", "（", "(", "一", "二", "三", "四", "五", "六", "七", "八", "九",
                  "各类", "上述", "以下", "如下", "特此", "注", "说明",
                  "本", "该", "此", "其", "根据", "按照", "现将", "经", "为了", "结合",
                  "落实", "推进", "做好", "加强", "建立", "完善", "确保", "组织",
                  "坚持", "深化", "规范", "严格")
EXCLUDE_SUBSTR = ("是否", "各类", "上述", "特此", "附件", "附：", "附:",
                  "一式", "叁份", "贰份", "壹份", "留存", "备案", "签字", "签名",
                  "审批", "档案", "年月日", "如下", "以上")
START_OK = ("关于", "印发", "开展", "对", "2023", "2024", "公司", "本")
# 封面判定：整页极稀疏(纯标题页)即视为“单页大标题封面”(合集/分册封面)，前向合并到后续文档。
# COVER_PAGE_MAX：整页中文字数上限；COVER_BODY_MAX：标题行之后正文字数上限。
COVER_PAGE_MAX = 40
COVER_BODY_MAX = 12
COVER_MARKERS = ("汇编", "部分", "篇", "卷", "目录", "总册", "集")
GRID_CHARS = "│├┌┐└┘┬┴┼━─"


def norm(s):
    return re.sub(r"\s+", "", s or "")


def ocr_to_lines(img):
    """OCR 一帧，返回(按阅读顺序拼接的文本, 平均置信度)。用置信度判断朝向是否正确。
    按 OCR_BACKEND 分派：rapidocr 走 ONNX 引擎（返回每行文本与分数），tesseract 走 image_to_data。"""
    if OCR_BACKEND == "tesseract":
        return _ocr_to_lines_tesseract(img)
    try:
        return _ocr_to_lines_rapid(img)
    except ImportError:
        return _ocr_to_lines_tesseract(img)


def _ocr_to_lines_tesseract(img):
    d = pytesseract.image_to_data(img, lang=TESS_LANG, output_type=Output.DICT)
    lines_map = {}
    for i in range(len(d["text"])):
        w = (d["text"][i] or "").strip()
        if not w:
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        lines_map.setdefault(key, []).append(w)
    lines = [" ".join(ws) for ws in lines_map.values()]
    confs = [int(c) for c in d["conf"] if str(c) not in ("", "-1")]
    mean_conf = sum(confs) / len(confs) if confs else 0
    return "\n".join(lines), mean_conf


def _ocr_to_lines_rapid(img):
    import numpy as np
    engine = _rapid_ocr_engine()
    arr = np.array(img.convert("RGB"))[:, :, ::-1]
    result, _ = engine(arr)
    if not result:
        return "", 0.0
    texts = [str(line[1]) for line in result]
    scores = [float(line[2]) for line in result if len(line) > 2 and line[2] is not None]
    mean_conf = sum(scores) / len(scores) if scores else 0.0
    return "\n".join(texts), mean_conf


# ---- 可插拔 OCR 后端（默认 RapidOCR 离线识别；缺失时自动回退 Tesseract）----
_RAPID_ENGINE = None
_TESS_FALLBACK_WARNED = False


def _tesseract_text(img, lang, config):
    return pytesseract.image_to_string(img, lang=lang, config=config or "")


def _rapid_ocr_engine():
    """懒加载 RapidOCR 引擎（每个进程一份单例，避免重复加载 ONNX 模型）。"""
    global _RAPID_ENGINE
    if _RAPID_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _RAPID_ENGINE = RapidOCR()
    return _RAPID_ENGINE


def _rapid_ocr_text(img):
    """RapidOCR 识别：输入 PIL 图像，返回按检测顺序拼接的文本（无需 opencv，RGB->BGR 用 numpy 切片）。"""
    import numpy as np
    engine = _rapid_ocr_engine()
    arr = np.array(img.convert("RGB"))[:, :, ::-1]   # RGB -> BGR（RapidOCR 期望 BGR）
    result, _ = engine(arr)
    if not result:
        return ""
    return "\n".join(str(line[1]) for line in result)


def ocr_image_to_text(img, lang=TESS_LANG, config=OCR_CONFIG_FAST):
    """统一 OCR 入口：按 OCR_BACKEND 分派。RapidOCR 未安装时自动回退 Tesseract 并告警一次。
    输入 PIL 图像，返回识别文本。config 仅对 Tesseract 生效（RapidOCR 忽略）。"""
    if OCR_BACKEND == "tesseract":
        return _tesseract_text(img, lang, config)
    try:
        return _rapid_ocr_text(img)
    except ImportError:
        global _TESS_FALLBACK_WARNED
        if not _TESS_FALLBACK_WARNED:
            print("[!] 未安装 rapidocr_onnxruntime，回退 Tesseract OCR（建议：pip install rapidocr-onnxruntime）")
            _TESS_FALLBACK_WARNED = True
        return _tesseract_text(img, lang, config)


# 领域强特征词(多为多字)：判向用。竖版文档多含这些词；颠倒页OCR出的乱码基本不含。
# 注意：避免用单字(年/月/日/睿/畅等)，它们易在乱码中误中。
EXPECT_TOKENS = ["公司", "保密", "武汉", "睿畅", "科技", "有限", "责任", "责任书", "承诺书",
                "书", "表", "通知", "通报", "报告", "纪要", "制度", "规定", "办法", "细则",
                "计划", "工作", "人员", "部门", "领导", "小组", "检查", "考核", "登记",
                "法定代表人", "涉密", "归口", "承诺", "武汉睿畅"]


def domain_hits(text):
    s = re.sub(r"\s+", "", text or "")
    return sum(1 for tok in EXPECT_TOKENS if tok in s)


def _gray_pix(page, zoom, clip=None):
    kw = dict(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
    if clip is not None:
        kw["clip"] = clip
    return page.get_pixmap(**kw)


def _gray_image(pix):
    return Image.frombytes("L", (pix.width, pix.height), pix.samples)


def dark_ratio(doc, pno, zoom=BLANK_ZOOM, clip=None):
    """整页(或局部)暗像素占比：用于空白页快筛与封面稀疏判定，比 OCR 快数百倍。"""
    pix = _gray_pix(doc[pno - 1], zoom, clip)
    im = _gray_image(pix)
    n = pix.width * pix.height
    return sum(im.histogram()[:200]) / n if n else 1.0


def _bands_image(doc, pno, zoom, top_ratio, foot_ratio):
    """页顶标题窄带 + 页脚条带拼成一张灰度图：一次 OCR 同时拿到标题与页码。"""
    page = doc[pno - 1]
    W, H = page.rect.width, page.rect.height
    top = _gray_pix(page, zoom, fitz.Rect(0, 0, W, H * top_ratio))
    foot = _gray_pix(page, zoom, fitz.Rect(0, H * (1 - foot_ratio), W, H))
    w = max(top.width, foot.width)
    canvas = Image.new("L", (w, top.height + BAND_GAP + foot.height), 255)
    canvas.paste(_gray_image(top), (0, 0))
    canvas.paste(_gray_image(foot), (0, top.height + BAND_GAP))
    return canvas


def page_text_layer(doc, pno, min_cn=TEXT_LAYER_MIN_CN):
    """PDF 内嵌文本层：中文字符够多则直接采用，完全跳过 OCR（最快路径，质量也最好）。"""
    t = doc[pno - 1].get_text("text") or ""
    return t if len(re.findall(r"[\u4e00-\u9fff]", t)) >= min_cn else ""


def ocr_page_best(pdf_path, pno):
    """三级降本 OCR（由快到慢，命中即返回）：
    1) 内嵌文本层  -> 直接采用，零 OCR 成本；
    2) 空白页快筛  -> 低分辨率渲染，几乎全是白像素则跳过 OCR；
    3) 『页顶窄带(22%)+页脚(8%)』灰度图 + psm6 -> 标题与页码一次拿到；
       读不到有效文字才回退 50% 宽条带，并按 0/90/180/270 判向取领域词最多者。
    返回 (文本, 旋转角)。旋转角只代表识别时的取向，绝不用于修改 PDF 版面。"""
    doc = fitz.open(pdf_path)
    try:
        tl = page_text_layer(doc, pno)
        if tl:
            return tl, 0
        if dark_ratio(doc, pno) < BLANK_DARK_RATIO:
            return "", 0
        img = _bands_image(doc, pno, OCR_ZOOM, TOP_FAST_RATIO, FOOT_RATIO)
        fast = ocr_image_to_text(img, TESS_LANG, OCR_CONFIG_FAST)
        if domain_hits(fast) >= 1 and len(norm(fast)) >= 8:
            return fast, 0
        # 回退：宽条带重新识别（.title 在页中部/封面式排版/整页侧躺等）
        img = _bands_image(doc, pno, OCR_ZOOM, TOP_RATIO, FOOT_RATIO)
        t0 = ocr_image_to_text(img, TESS_LANG, "")
        h0 = domain_hits(t0)
        if h0 >= 1:
            return t0, 0
        best = (t0, 0, h0)
        for ang in (90, 180, 270):
            t = ocr_image_to_text(img.rotate(ang, expand=True), TESS_LANG, "")
            d = domain_hits(t)
            if d > best[2]:
                best = (t, ang, d)
        return best[0], best[1]
    finally:
        doc.close()


def _ocr_worker(pdf_pno):
    """多进程 worker：对单页做朝向感知 OCR，返回 (页码, 文本, 旋转角)。"""
    pdf_path, pno = pdf_pno
    text, ang = ocr_page_best(pdf_path, pno)
    return pno, text, ang


def load_ocr(pdf_path, progress_cb=None):
    """progress_cb(done, total)：每完成一页回调一次（GUI 进度条）；返回 False 表示用户取消，
    此时终止 OCR 进程池、保存已完成页缓存并抛 SegmentCancelled。"""
    # 缓存随 OCR 后端隔离：切换引擎（rapidocr/tesseract）不会误读另一引擎的结果
    tcache = pdf_path + f".{OCR_BACKEND}.ocr.json"
    ocache = pdf_path + f".{OCR_BACKEND}.osd.json"
    tdata, odata = {}, {}
    if os.path.exists(tcache):
        with open(tcache, "r", encoding="utf-8") as f:
            tdata = json.load(f)
    if os.path.exists(ocache):
        with open(ocache, "r", encoding="utf-8") as f:
            odata = json.load(f)
    doc = fitz.open(pdf_path)
    n = doc.page_count
    doc.close()
    todo = [(pdf_path, p) for p in range(1, n + 1)
            if not (str(p) in tdata and tdata[str(p)] and str(p) in odata and odata[str(p)] is not None)]
    base_done = n - len(todo)     # 缓存已命中页计入进度
    if todo:
        workers = min(8, max(1, mp.cpu_count() or 1))   # 每进程跑一个 OCR，吃满核
        if OCR_BACKEND == "rapidocr":
            workers = min(4, workers)   # RapidOCR 每进程加载一份模型，限并行防内存压力
        with mp.Pool(processes=workers) as pool:
            done = 0
            total = len(todo)
            cancelled = False
            for pno, text, ang in pool.imap_unordered(_ocr_worker, todo):
                tdata[str(pno)] = text
                odata[str(pno)] = ang
                done += 1
                if progress_cb and progress_cb(base_done + done, n) is False:
                    cancelled = True
                    break
                if done % 20 == 0:
                    with open(tcache, "w", encoding="utf-8") as f:
                        json.dump(tdata, f, ensure_ascii=False)
                    with open(ocache, "w", encoding="utf-8") as f:
                        json.dump(odata, f, ensure_ascii=False)
            with open(tcache, "w", encoding="utf-8") as f:
                json.dump(tdata, f, ensure_ascii=False)
            with open(ocache, "w", encoding="utf-8") as f:
                json.dump(odata, f, ensure_ascii=False)
            if cancelled:
                pool.terminate()
                raise SegmentCancelled(pdf_path)
    elif progress_cb:
        progress_cb(n, n)
    return tdata


ATT_RE = re.compile(r"^附\s*件\s*\d*\s*[：:、.．]?\s*(.*)$")
# 编号/文号/日期/页码类噪声行（附件标记行之后常紧跟这些，不是标题）
NONTITLE_PREFIX = ("编号", "文号", "序号", "档案号", "文件号", "密级", "份号", "页码",
                   "NO", "No", "no", "N0", "N0.", "NO.", "No.")
NONTITLE_RE = re.compile(r"^(编号|文号|序号|档案号|文件号|密级|份号|页码|NO|No|no|N0)\s*[:：.]?", re.I)
PURE_NUM_RE = re.compile(r"^[\d\W_]+$")
DATE_LINE_RE = re.compile(r"^(19|20)\d{2}\s*[年\-/.]\s*\d{1,2}\s*[月\-/.]\s*\d{1,2}\s*日?$")
PAGE_LINE_RE = re.compile(r"^第?\s*\d+\s*页?(\s*(/|共)\s*\d+\s*页?)?$")


def looks_like_title(nl):
    """判断一行是否像『文档标题』：排除编号/文号/日期/页码/纯数字等噪声行。
    附件页常见排版：『附件1』 → 『编号：XXX』(噪声) → 『真实标题』，靠此函数跳过噪声。"""
    if not nl or len(nl) < 3 or len(nl) > 40:
        return False
    if not re.search(r"[\u4e00-\u9fff]", nl):   # 无中文：多为编号/英文代号
        return False
    if NONTITLE_RE.match(nl) or nl.startswith("编号"):
        return False
    if PURE_NUM_RE.match(nl) or DATE_LINE_RE.match(nl) or PAGE_LINE_RE.match(nl):
        return False
    if any(p in nl for p in "，。；：、（）·！？…—"):
        return False
    return True


def find_attachment_title(lines):
    """在给定行中找『附件』标题行：以『附件』开头即视为附件起始页。
    - 附件行自带名称(且非编号) -> 直接用该行；
    - 仅『附件N』标记(或后面跟的是编号/文号) -> 向下找第一个像标题的行
      （优先含文档/表单关键词的行），跳过编号、日期、页码等噪声行；
    - 始终找不到 -> 退回附件标记行本身(保留『附件N』)，绝不把编号当标题。"""
    for i, l in enumerate(lines):
        nl = norm(l)
        if not (nl.startswith("附件") or re.match(r"^附\s*件\s*\d", nl)):
            continue
        m = ATT_RE.match(nl)
        rest = norm(m.group(1)) if m else ""
        if rest and looks_like_title(rest):
            return l
        rest_lines = [x for x in lines[i + 1:i + 10] if x.strip()]
        for nxt in rest_lines:                       # 优先：含文档/表单关键词的标题行
            if looks_like_title(norm(nxt)) and any(k in norm(nxt) for k in DOC_KW + FORM_KW):
                return nxt
        for nxt in rest_lines:                       # 兜底：第一个像标题的行
            if looks_like_title(norm(nxt)):
                return nxt
        return l
    return None


def page_has_attachment(text):
    """页首若干行出现『附件N』标记 -> 视为附件起始页（附件须单独成篇，不并入前文）。"""
    for l in [x.strip() for x in (text or "").splitlines() if x.strip()][:10]:
        nl = norm(l)
        if nl.startswith("附件") or re.match(r"^附\s*件\s*\d", nl):
            return True
    return False


def extract_title(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    comp_i = next((i for i, l in enumerate(lines) if "公司" in l), None)
    search = lines[comp_i + 1:] if comp_i is not None else lines

    # 1) 附件优先（一般首行有『附件』字样，标题在其后几行）
    att = find_attachment_title(lines[:10])
    if att:
        return att

    def is_title_line(nl):
        if len(nl) < 4 or len(nl) > 30:
            return False
        if not re.search(r"[\u4e00-\u9fff]", nl):
            return False
        if NONTITLE_RE.match(nl) or PURE_NUM_RE.match(nl):   # 编号/文号/纯数字行不是标题
            return False
        if any(p in nl for p in "，。；：、（）·！？…—.:;!?"):
            return False
        if nl.startswith(EXCLUDE_PREFIX) or any(s in nl for s in EXCLUDE_SUBSTR):
            return False
        # 文档型：含文档关键词，且(以起始词开头 / 以关键词结尾 / 关键词在末4字)
        if any(k in nl for k in DOC_KW):
            if nl.startswith(START_OK) or nl.endswith(tuple(DOC_KW)) or any(k in nl[-4:] for k in DOC_KW):
                return True
        # 表单型：必须以表单关键词结尾（如「涉密人员登记表」）
        if nl.endswith(tuple(FORM_KW)):
            return True
        return False

    for l in search[:6]:
        if is_title_line(norm(l)):
            return l
    for l in lines[:6]:
        if is_title_line(norm(l)):
            return l
    return None


# ---- 标题自动校正（OCR 形近错字/乱码归一）----
STD_COMPANY = "武汉睿畅科技有限公司"
STD_TYPES = ["责任书", "承诺书", "审查表", "考核表", "记录表", "登记表", "培训计划",
             "会议纪要", "例会", "申请", "通知", "方案", "办法", "制度", "规定",
             "细则", "计划", "报告", "总结", "公告", "声明", "决议", "决定",
             "讲话", "通报", "意见", "批复", "条例", "规程", "规范",
             "手册", "清单", "花名册", "台账", "名册", "统计表", "汇总表", "检查表"]
# 参与「编辑距离<=1 模糊校正」的子集：只用 3 字及以上的类型词。
# 双字词(例会/通知/制度/办法…)任意两字的编辑距离都可能<=1，模糊匹配极易误伤真词
# （如「总经理办公会纪要」→「…办例会纪要」、「保密检查的」→「…检查表」），故排除，
# 只保留精确命中（find_docs 用 `k in t` 判定，不需要强行改写）。
_FUZZY_STD_TYPES = [w for w in STD_TYPES if len(w) >= 3]
TAIL_PUNCT = "。、，；：！？…—.,;!?)）]》"
# 高频 OCR 形近错字硬映射（运行中发现即补充）
OCR_FIX = {"货任书": "责任书", "货任": "责任", "审跋表": "审查表", "农具": "家具",
           "屏菲柜": "屏蔽柜", "和昶睿达": "睿畅", "和昶睿畅": "睿畅",
           "函于": "关于", "函汉": "武汉", "有限公告": "有限公司", "作刊雨": "",
           # 注："检查表" 是合法文档名(且与 FORM_KW 结尾判定相关)，不可再映射到"审查表"
           # 双字连续形近误（编辑距离2，用上下文特定映射避免误伤真词）
           "计划机": "计算机", "总结理": "总经理", "半制度": "半年度",
           "保密办法": "保密办公", "制度归口": "年度归口", "刻制度密": "刻制保密",
           "决定密": "定密", "声明确": "明确", "公告名册": "公司名称",
           "通报书": "通知书", "述职公告": "述职报告", "2023制度": "2023年度",
           "2024制度": "2024年度", "审查决定": "审查认定",
           "领导条例决定纪要": "领导小组会议纪要", "第一条例会": "第一次例会",
           "第二条例会": "第二次例会", "全体员条例会决定纪要": "全体员工会议纪要",
           "工作机条例决定": "工作机构"}


def _edit_dist(a, b):
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 1:
        return 99
    la, lb = len(a), len(b)
    dp = list(range(lb + 1))
    for i in range(1, la + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, lb + 1):
            cur = dp[j]
            cost = 0 if a[i - 1] == b[j - 1] else 1
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev + cost)
            prev = cur
    return dp[lb]


def _std_mask(text, words):
    """标记 text 中已经是标准词的字符区间(True 表示受保护)，避免模糊校正把正确的词改错。
    例：STD_TYPES 里同时有「通知」和「通报」时，若无保护会把正确的「通知」改成「通报」。"""
    mask = [False] * len(text)
    for w in words:
        start = 0
        while True:
            i = text.find(w, start)
            if i < 0:
                break
            for k in range(i, i + len(w)):
                mask[k] = True
            start = i + 1
    return mask


def _fuzzy_replace(text, std, max_dist, protect=None, tail_at=None):
    """在 text 中找与 std 编辑距离<=max_dist 的等长子串并替换为 std。
    protect：True 表示该位置已是某个标准词，跳过不替换。
    tail_at：给了整数 n 时，只替换紧贴标题末尾的子串(i+L == n)，即只校正类型尾词，
    避免在标题中间乱改（如「保密检查的」被按「检查表」改成「保密审查表」）。"""
    L = len(std)
    if L < 2:                      # 单字不做模糊替换，避免任意单字被误改为标准词(如"函")
        return text
    if L == 0 or len(text) < L:
        return text
    best = None
    for i in range(len(text) - L + 1):
        if tail_at is not None and i + L != tail_at:
            continue
        sub = text[i:i + L]
        if sub == std:
            return text
        if protect and any(protect[i:i + L]):
            continue
        d = _edit_dist(sub, std)
        if d <= max_dist and (best is None or d < best[0]):
            best = (d, i)
    if best:
        i = best[1]
        return text[:i] + std + text[i + L:]
    return text


def correct_title(t):
    """OCR 标题自动校正：公司名归一 + 文档类型尾词形近校正 + 高频错字硬映射。"""
    if not t:
        return t
    s = norm(t)
    # 1) 公司名归一（容错各种变体：和昶睿达/有陬公司…）
    s = re.sub(r"武汉.{0,10}科技.{0,8}(有限)?公司", STD_COMPANY, s)
    # 2) 文档类型尾词形近校正（>=3字词、编辑距离<=1，只校正紧贴末尾的类型词）；
    #    已是标准词的位置受保护，不会被改成另一个标准词
    tail_at = len(s.rstrip(TAIL_PUNCT))
    for std in _FUZZY_STD_TYPES:
        s = _fuzzy_replace(s, std, 1, _std_mask(s, STD_TYPES), tail_at=tail_at)
    # 3) 高频错字硬映射
    for bad, good in OCR_FIX.items():
        if bad in s:
            s = s.replace(bad, good)
    return s


def page_is_cover(text, doc=None, pno=None):
    """单页大标题封面(多为合集/分册封面)：整页极稀疏(纯标题页)即判为封面，前向合并到后续文档。
    稀疏判定优先用「整页暗像素占比」（传 doc+pno），因为 OCR 只取窄条带，文本量已不能代表整页。
    排除：附件页、表单页、含具体文档关键词(DOC_KW)的文档标题页——它们本会作为文档起点，
    正文作为续页并入，已保证完整；只有“非具体文档的纯标题页/合集标记”才前向合并。"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    if any(c in text for c in GRID_CHARS):
        return False
    if page_has_attachment(text):
        return False          # 附件单独成篇，不判封面
    t = extract_title(text)
    nt = norm(t) if t else None
    if nt:
        if nt.startswith("附件") or re.match(r"^附\s*件", nt):
            return False          # 附件单独成篇
        if nt.endswith(tuple(FORM_KW)):
            return False          # 表单单独成篇
        if any(k in nt for k in DOC_KW):
            return False          # 具体文档标题页不判封面(其正文会作为续页并入,已完整)
        if any(m in nt for m in COVER_MARKERS):
            return True
    # 通用规则：整页极稀疏的纯标题页 -> 封面
    if doc is not None and pno is not None:
        return dark_ratio(doc, pno) < SPARSE_DARK_RATIO
    first = norm(lines[0])
    body = "".join(lines[1:])
    if len(norm(text)) <= COVER_PAGE_MAX and len(norm(body)) <= COVER_BODY_MAX:
        return True
    return False


def title_key(t):
    """标题指纹：仅保留中文与数字，消除空格/下划线/标点与OCR噪声。"""
    return re.sub(r"[^\u4e00-\u9fff0-9]", "", t or "")


# ---- 页码模式（借鉴 unstaple：页码重置是“新文档起点”的强信号）----
PN_RE_ZH = re.compile(r"第\s*([0-9零一二三四五六七八九十百两]+)\s*页\s*(?:共\s*([0-9零一二三四五六七八九十百两]+)\s*页)?", re.I)
PN_RE_EN = re.compile(r"page\s+(\d+)\s+of\s+(\d+)", re.I)
_CN = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6,
       '七': 7, '八': 8, '九': 9, '十': 10, '百': 100}


def _cn_to_int(s):
    if s.isdigit():
        return int(s)
    total, cur = 0, 0
    for ch in s:
        if ch in '0123456789':
            cur = cur * 10 + int(ch)
        elif ch == '十':
            cur = (cur or 1) * 10
        elif ch == '百':
            cur = (cur or 1) * 100
        elif ch in _CN:
            cur = _CN[ch]
        else:
            return None
        if ch in ('十', '百'):
            total += cur
            cur = 0
    total += cur
    return total or None


def parse_page_number(text):
    """从页文本提取页码 (cur, total)，仅识别明确模式：
    中文「第X页共Y页」、英文「Page X of Y」。页码多在页脚，取最后一个匹配。
    返回 (cur, total) 或 None（total 可能为 None）。"""
    m = None
    for mm in PN_RE_ZH.finditer(text or ""):
        m = mm
    if m:
        cur = _cn_to_int(m.group(1))
        total = _cn_to_int(m.group(2)) if m.group(2) else None
        if cur is not None:
            return (cur, total)
    m = PN_RE_EN.search(text or "")
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def find_docs(ocr, verbose=False):
    """定位文档边界。综合两类信号（借鉴 unstaple 的相邻页决策思路）：
    1) 标题行法：含文档/表单/附件标题行即可能是新文档起点；与上一边界标题近同则视为续页。
    2) 页码重置法：当某页是「第N页共M页」(N==M，文档末页) 且下一页回到「第1页」时，
       下一页强制为新文档起点（覆盖标题相似的误合并，非对称优先）。
    两者叠加，既补强“无标准红头标题的新文档起点”检测，又加固续页不误切。
    返回 [(页码, 标题, 证据), ...]。"""
    import difflib
    pn = {int(p): parse_page_number(ocr[p]) for p in ocr}
    pages_sorted = sorted(ocr, key=lambda x: int(x))
    forced = set()
    for i, p in enumerate(pages_sorted):
        cur, total = pn[int(p)] or (None, None)
        if total and cur == total:                     # 本页是某文档最后一页
            nxt = pages_sorted[i + 1] if i + 1 < len(pages_sorted) else None
            if nxt is not None:
                nc, _ = pn[int(nxt)] or (None, None)
                if nc == 1:                            # 下一页回到第1页 -> 新文档
                    forced.add(int(nxt))
    docs = []
    for p in pages_sorted:
        t0 = extract_title(ocr[p])
        if not t0:
            continue
        t = correct_title(t0)
        # 乱码/无效标题过滤：校正后仍无文档类型词/附件/公司名 -> 不单独成篇(并入前文档,保持完整)
        # 附件页例外：即使标题不含标准关键词也单独成篇(用户要求附件独立)
        if not (any(k in t for k in STD_TYPES)
                or "附件" in t or STD_COMPANY in t
                or page_has_attachment(ocr[p])):
            continue
        k = title_key(t)
        ev = "标题"
        if docs:
            ok = title_key(docs[-1][1])
            sim = difflib.SequenceMatcher(None, k, ok).ratio()
            # 与上一边界标题完全相同/高度相似(≥0.85)/候选为其截断子串(k in ok) -> 续页。
            # 不用 ok in k(反向)，避免把"市场部负责人保密考核表"误并入"保密考核表"。
            if k == ok or sim >= 0.85 or k in ok:
                if int(p) in forced:
                    ev = "页码重置(上一文档末页→本页第1页)"
                else:
                    continue
        docs.append((int(p), t, ev))
    return docs


def check_layout_preserved(src_doc, out_path, pages):
    """校验输出文件是否原样保持了源页版面（旋转角与页面尺寸）。
    本软件只切分、不改版面：任何一页方向/尺寸与源页不一致都说明被旋转或栅格化过。
    返回不一致的页数（-1 表示无法校验）。"""
    try:
        d = fitz.open(out_path)
        bad = 0
        for i, pno in enumerate(pages):
            if i >= d.page_count:
                bad += 1
                break
            sp, op = src_doc[pno - 1], d[i]
            # 只比显示尺寸：方向错会导致宽高互换（>1 即捕到）；产物 /Rotate=0 故不比 rotation 元数据
            if (abs(op.rect.width - sp.rect.width) > 1
                    or abs(op.rect.height - sp.rect.height) > 1):
                bad += 1
        d.close()
        return bad
    except Exception:
        return -1


def segment(pdf_path, out_dir, progress_cb=None):
    ocr = load_ocr(pdf_path, progress_cb)
    npages = len(ocr)
    docs = find_docs(ocr)
    print(f"[*] {os.path.basename(pdf_path)}  共{npages}页, 命中红头文档 {len(docs)} 篇")
    for p, t, ev in docs:
        print(f"    p{p}: {t}  [{ev}]")
    doc = fitz.open(pdf_path)
    base = re.sub(r"\.pdf$", "", os.path.basename(pdf_path), flags=re.I)
    out_root = os.path.join(out_dir, base)
    os.makedirs(out_root, exist_ok=True)
    # 候选片段：起点 = 第1页 或 各边界页（去重，避免 p1 同时作为隐式起点与边界导致零页）
    bounds = [1] + [p for p, _, _ in docs]
    uniq = []
    for s in bounds:
        if not uniq or s != uniq[-1]:
            uniq.append(s)
    bounds = uniq
    segs = []
    title_of = {p: t for p, t, _ in docs}   # 起点页 -> 该页命中的标题
    for i, s in enumerate(bounds):
        end = bounds[i + 1] - 1 if i + 1 < len(bounds) else npages
        # 段名取「本段起点页自己的标题」；只有隐式首段(第1页未命中标题，如册封面)才用册名。
        # （旧写法用 docs[i-1] 会导致标题整体错位一位）
        title = title_of.get(s, base)
        segs.append([s, end, title])
    # 封面合并：把“单页大标题封面”段并入其后续文档（作为前导页），不单独成篇。
    # 先用各段“原始起点”预判定封面，避免合并后起点变化导致级联误并。
    cover_flag = [page_is_cover(ocr[str(seg[0])], doc, seg[0]) for seg in segs]
    merged = []
    for idx, seg in enumerate(segs):
        s, e, t = seg
        # 限制页数：只合并 1-2 页的封面，避免把真实文档误当封面吞掉
        if idx < len(segs) - 1 and cover_flag[idx] and (e - s + 1) <= COVER_MAX_PAGES:
            segs[idx + 1][0] = min(segs[idx + 1][0], s)   # 后续文档起点前移包含封面
            continue
        merged.append(seg)
    segs = merged
    manifest = []
    for i, (s, end, title) in enumerate(segs, start=1):
        # 空白页检测（借鉴 unstaple：扫描分隔纸/双面空白背面归属其所在文档，不单独成篇）。
        # 段内全部为空白页 -> 跳过输出，避免生成空文件；部分空白则保留空白页随文档。
        seg_pages = list(range(s, end + 1))
        blank = [pp for pp in seg_pages if len(norm(ocr.get(str(pp), ""))) < 4]
        if len(blank) == len(seg_pages):
            print(f"    - 跳过 纯空白段 p{s}-{end}（{title}）")
            continue
        newdoc = fitz.open()
        # 逐页输出：只做切分、绝不旋转/转正。
        # 用 show_pdf_page 把源页按【显示尺寸】矢量绘制到新页——内容保持源页原始朝向，
        # 且产物统一 /Rotate=0，彻底规避 /Rotate 元数据在不同阅读器下的解释差异。
        # （仍是矢量复制，不栅格化；与铁律一致：只切分、不改版面。）
        for pp in range(s, end + 1):
            sp = doc[pp - 1]
            r = sp.rect  # 已展开 /Rotate 后的显示尺寸
            newdoc.new_page(width=r.width, height=r.height)
            newdoc[-1].show_pdf_page(r, doc, pp - 1)
        safe = re.sub(r'[\\/:*?"<>|]', "_", norm(title))
        safe = safe.strip("，。,.;；：:！!?？、（）()_ ").strip()[:40]
        out_name = f"{safe}.pdf"
        out_path = os.path.join(out_root, out_name)
        # 避免重名
        c = 1
        while os.path.exists(out_path):
            out_path = os.path.join(out_root, f"{safe}_{c}.pdf")
            c += 1
        newdoc.save(out_path)
        newdoc.close()
        bad = check_layout_preserved(doc, out_path, seg_pages)
        if bad:
            print(f"    ! 警告：{os.path.basename(out_path)} 有 {bad} 页方向与源页不一致（版面被改动）")
        manifest.append({"index": i, "title": title, "pages": f"{s}-{end}", "out": out_path})
    doc.close()
    man_path = os.path.join(out_root, "manifest.csv")
    with open(man_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["index", "title", "pages", "out"])
        w.writeheader()
        w.writerows(manifest)
    print(f"    输出 {len(manifest)} 个文件 -> {out_root}")
    return manifest


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", default=r"D:\Backup\RayChan\split_redhead")
    args = ap.parse_args()
    segment(args.pdf, args.out)

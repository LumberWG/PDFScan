"""红头文档级切分：基于OCR文本，定位每个红头独立文档(例会/纪要/通知/报告…)的起始页，
按边界切片并以其标题命名。文件名仅作为装订册名(输出文件夹)。
用法: python segment_redhead.py <pdf> [--out DIR]
"""
import sys, os, re, json, csv, argparse, io, shutil, fitz
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
ZOOM = 2.0
OCR_ZOOM = 1.6          # OCR 仅识别顶部条带并降分辨率（拆分只需每页前几行）
TOP_RATIO = 0.5         # 只渲染页面顶部 50% 区域（红头公司名+标题+前几行均在此）
OCR_CACHE_SUFFIX = ".ocr.json"
OSD_CACHE_SUFFIX = ".osd.json"


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
    """OCR 一帧，返回(按阅读顺序拼接的文本, 平均置信度)。用置信度判断朝向是否正确。"""
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


# 领域强特征词(多为多字)：判向用。竖版文档多含这些词；颠倒页OCR出的乱码基本不含。
# 注意：避免用单字(年/月/日/睿/畅等)，它们易在乱码中误中。
EXPECT_TOKENS = ["公司", "保密", "武汉", "睿畅", "科技", "有限", "责任", "责任书", "承诺书",
                "书", "表", "通知", "通报", "报告", "纪要", "制度", "规定", "办法", "细则",
                "计划", "工作", "人员", "部门", "领导", "小组", "检查", "考核", "登记",
                "法定代表人", "涉密", "归口", "承诺", "武汉睿畅"]


def domain_hits(text):
    s = re.sub(r"\s+", "", text or "")
    return sum(1 for tok in EXPECT_TOKENS if tok in s)


def ocr_page_best(pdf_path, pno):
    """逐页检测朝向：竖版识别结果含≥2领域强词即判竖版(1次)；否则试转 180→90→270 取领域词最多者。
    返回 (校正后的文本, 校正旋转角)。用领域词命中数判向，不受OCR置信度波动影响。"""
    doc = fitz.open(pdf_path)
    page = doc[pno - 1]
    # 只渲染顶部条带：拆分只需每页前几行（公司名+标题+页码多在页首），省去整页OCR开销
    clip = fitz.Rect(0.0, 0.0, page.rect.width, page.rect.height * TOP_RATIO)
    pix = page.get_pixmap(matrix=fitz.Matrix(OCR_ZOOM, OCR_ZOOM), clip=clip)
    doc.close()
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    # 仅以领域强词判向：竖版(含标题页/续页)通常至少命中 1 个领域词 -> 直接返回不做旋转重试。
    # 仅当完全读不到领域词(可能整页横置/颠倒)时，试转 90/180/270 取领域词命中最多者，
    # 以正确识别横向(90/270)扫描页，避免切片时漏转正导致"竖版变横版"。
    t0 = pytesseract.image_to_string(img, lang=TESS_LANG)
    h0 = domain_hits(t0)
    if h0 >= 1:
        return t0, 0
    best = (t0, 0, h0)
    for ang in (90, 180, 270):
        im = img.rotate(ang, expand=True)
        t = pytesseract.image_to_string(im, lang=TESS_LANG)
        d = domain_hits(t)
        if d > best[2]:
            best = (t, ang, d)
    return best[0], best[1]


def _ocr_worker(pdf_pno):
    """多进程 worker：对单页做朝向感知 OCR，返回 (页码, 文本, 旋转角)。"""
    pdf_path, pno = pdf_pno
    text, ang = ocr_page_best(pdf_path, pno)
    return pno, text, ang


def load_ocr(pdf_path, progress_cb=None):
    """progress_cb(done, total)：每完成一页回调一次（GUI 进度条）；返回 False 表示用户取消，
    此时终止 OCR 进程池、保存已完成页缓存并抛 SegmentCancelled。"""
    tcache = pdf_path + OCR_CACHE_SUFFIX
    ocache = pdf_path + OSD_CACHE_SUFFIX
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
        workers = min(6, max(1, mp.cpu_count() or 1))
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


def load_osd(pdf_path):
    ocache = pdf_path + OSD_CACHE_SUFFIX
    if os.path.exists(ocache):
        with open(ocache, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


ATT_RE = re.compile(r"^附\s*件\s*\d*\s*[：:、.．]?\s*(.*)$")


def find_attachment_title(lines):
    """在给定行中找『附件』标题行：以『附件』开头即视为附件起始页。
    返回该行(含附件标记)作为标题；若仅『附件N』标记行无名称，则用下一行作标题。"""
    for i, l in enumerate(lines):
        nl = norm(l)
        if nl.startswith("附件") or re.match(r"^附\s*件", nl):
            m = ATT_RE.match(nl)
            rest = m.group(1) if m else ""
            if rest:
                return l
            for nxt in lines[i + 1:]:
                if nxt.strip():
                    return nxt
    return None


def extract_title(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    comp_i = next((i for i, l in enumerate(lines) if "公司" in l), None)
    search = lines[comp_i + 1:] if comp_i is not None else lines

    # 1) 附件优先（一般首行有『附件』字样，标题在前几行）
    att = find_attachment_title(search) or find_attachment_title(lines[:6])
    if att:
        return att

    def is_title_line(nl):
        if len(nl) < 4 or len(nl) > 30:
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
# 高频 OCR 形近错字硬映射（运行中发现即补充）
OCR_FIX = {"货任书": "责任书", "货任": "责任", "审跋表": "审查表", "农具": "家具",
           "屏菲柜": "屏蔽柜", "和昶睿达": "睿畅", "和昶睿畅": "睿畅",
           "函于": "关于", "函汉": "武汉", "有限公告": "有限公司", "作刊雨": "",
           "检查表": "审查表",
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


def _fuzzy_replace(text, std, max_dist):
    """在 text 中找与 std 编辑距离<=max_dist 的等长子串并替换为 std。"""
    L = len(std)
    if L < 2:                      # 单字不做模糊替换，避免任意单字被误改为标准词(如"函")
        return text
    if L == 0 or len(text) < L:
        return text
    best = None
    for i in range(len(text) - L + 1):
        sub = text[i:i + L]
        if sub == std:
            return text
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
    # 2) 文档类型尾词形近校正（编辑距离<=1）
    for std in STD_TYPES:
        s = _fuzzy_replace(s, std, 1)
    # 3) 高频错字硬映射
    for bad, good in OCR_FIX.items():
        if bad in s:
            s = s.replace(bad, good)
    return s


def page_is_cover(text):
    """单页大标题封面(多为合集/分册封面)：整页极稀疏(纯标题页)即判为封面，前向合并到后续文档。
    排除：附件页、表单页、含具体文档关键词(DOC_KW)的文档标题页——它们本会作为文档起点，
    正文作为续页并入，已保证完整；只有“非具体文档的纯标题页/合集标记”才前向合并。"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    if any(c in text for c in GRID_CHARS):
        return False
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
        if not (any(k in t for k in STD_TYPES)
                or "附件" in t or STD_COMPANY in t):
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


def segment(pdf_path, out_dir, progress_cb=None):
    ocr = load_ocr(pdf_path, progress_cb)
    osd = load_osd(pdf_path)
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
    for i, s in enumerate(bounds):
        end = bounds[i + 1] - 1 if i + 1 < len(bounds) else npages
        title = docs[i - 1][1] if i > 0 else base
        segs.append([s, end, title])
    # 封面合并：把“单页大标题封面”段并入其后续文档（作为前导页），不单独成篇。
    # 先用各段“原始起点”预判定封面，避免合并后起点变化导致级联误并。
    cover_flag = [page_is_cover(ocr[str(seg[0])]) for seg in segs]
    merged = []
    for idx, seg in enumerate(segs):
        s, e, t = seg
        if idx < len(segs) - 1 and cover_flag[idx]:
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
        # 逐页输出：竖版页矢量复制，旋转页栅格化转正(消除"横版")
        for pp in range(s, end + 1):
            src_page = doc[pp - 1]
            src_rot = src_page.rotation       # 源页自带旋转(0/90/180/270)
            osd_ang = osd.get(str(pp), 0)     # OCR 判向补充角(0/180)
            eff = (src_rot + osd_ang) % 360   # 最终视觉旋转
            if eff == 0:
                # 正立 -> 矢量复制(文字可选中、体积小)
                newdoc.insert_pdf(doc, from_page=pp - 1, to_page=pp - 1)
            else:
                # 含旋转(含源 PDF 自带 /Rotate=90/270 横版) -> 栅格化转正,
                # 消除"竖版变横版"：get_pixmap 默认已应用 src_rot, 再叠加 osd 判向角
                print(f"    - 转正旋转页 p{pp} (源旋转={src_rot}, 判向={osd_ang})")
                pix = doc[pp - 1].get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM))
                im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                if osd_ang:
                    im = im.rotate(osd_ang, expand=True)
                buf = io.BytesIO()
                im.save(buf, "PNG")
                np = newdoc.new_page(width=im.width, height=im.height)
                np.insert_image(np.rect, stream=buf.getvalue())
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

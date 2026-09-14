# PDFScan — 批量扫描 PDF 的红头文档级切分工具

把一册批量扫描的 PDF（内含多份独立红头文档，如例会纪要、通知、办法、责任书、各类表/单）自动识别并切分为独立 PDF，并按每份文档的标题命名。

> 关键认知：文件名里的「3-1 保密工作领导小组」是**装订册名**，不是文档名。PDF 内部每份**红头文件**（公司名 + 大字号标题 + 日期）才是独立文档，必须按红头文档细切。

---

## 环境要求

- Python 3.12（Windows arm64 实测）
- Tesseract OCR v5.x，安装到 `C:\Program Files\Tesseract-OCR\tesseract.exe`，中文包 `chi_sim.traineddata` 置于其 `tessdata\`
- Python 依赖：`pymupdf`(fitz)、`pytesseract`、`Pillow`、`numpy`（视觉法 `detect_redhead` 需要）；运行 `server.py` 还需 `fastapi`、`uvicorn`、`python-multipart`
- 注：本机 Windows arm64，opencv / RapidOCR / PaddleOCR 等无预编译轮子，故本地中文 OCR 采用 **Tesseract**（x86_64 模拟运行，单页约 3–5 秒）。

---

## 目录结构（清理后）

```
PDFScan/
├── segment_redhead.py   # 核心：单册 PDF 的红头文档级切分
├── batch_redhead.py     # 批量：对 D:\Backup\RayChan 下所有 PDF 跑切分
├── gui.py               # Windows 图形界面：选文件/目录切分 + 实时进度条
├── detect_redhead.py    # 视觉法：检测红头封面候选页 + 裁剪 OCR 标题
├── server.py            # 离线 HTTP 接口（FastAPI，对外提供切分能力）
└── README.md
```

## 快速开始（从 GitHub 克隆后运行）

```bash
git clone https://github.com/LumberWG/PDFScan.git
cd PDFScan

# 1) Python 依赖
pip install pymupdf pytesseract Pillow numpy fastapi uvicorn python-multipart

# 2) Tesseract（二选一）
#    A. 系统安装: Tesseract v5.x 装到 C:\Program Files\Tesseract-OCR，中文包 chi_sim 放其 tessdata\
#    B. 便携捆绑(免安装): 把已装目录整目录复制到 vendor\tesseract\，脚本自动优先使用
#       New-Item -ItemType Directory -Force -Path vendor\tesseract
#       Copy-Item "C:\Program Files\Tesseract-OCR\*" vendor\tesseract\ -Recurse

# 3) 运行
python gui.py                  # Windows 图形界面（选文件/目录 + 实时进度条）
python server.py               # 离线 HTTP 接口 (http://127.0.0.1:8000)
python segment_redhead.py <pdf>  # 或命令行直接切分
```

HTTP 接口端点：`/health`、`/split`、`/split/upload`、`/download`（详见 `server.py`）。

---

产物输出到 `D:\Backup\RayChan\split_redhead\<册名>\`，每册一个 `manifest.csv`。

---

## 功能一：红头文档级切分（`segment_redhead.py`）

对**单册** PDF 执行完整切分流程：

1. **逐页 OCR（带朝向校正）** — `load_ocr`
   - Tesseract 对每页做中文 OCR；用领域强特征词（公司 / 保密 / 武汉睿畅 …）判断页面朝向，颠倒页自动尝试旋转 180°/90°/270° 取领域词最多者。
   - 结果缓存为 `<pdf>.ocr.json`（文本）与 `<pdf>.osd.json`（旋转角），可断点续跑、重跑复用。
2. **标题提取** — `extract_title`
   - 在「公司名之后的前 6 行」中找文档标题行。判定规则：
     - 文档型：含文档关键词（例会/纪要/通知/办法/责任书…），且以起始词开头 / 以关键词结尾 / 关键词在末 4 字；长度 4–30 字、无句中标点。
     - 表单型：以 表/单/书/名册/清单… 结尾（如「涉密人员登记表」）。
   - 大量排除词（附件/本/该/根据/落实…，以及序号、括号开头）拦截页脚与正文误报。
3. **边界定位** — `find_docs`（综合两类信号，思路借鉴 [unstaple](https://github.com/BenMalaga/unstaple) 的相邻页决策）
   - **标题行法**：仅「含标题行」的页才可能是新文档起点；若其标题与上一边界标题相同 / 高度相似(≥0.85) / 为其截断子串，则视为同一文档续页（解决「切得太碎」——表单表头每页重复、长文档跨页）。
   - **页码重置法**：自动识别中英文页码「第X页共Y页」「Page X of Y」；当某页是该文档末页(X==Y)且下一页回到「第1页」，下一页**强制**为新文档起点（非对称优先，覆盖标题相似的误合并）。这补强了「无标准红头标题的新文档起点」检测，并加固续页不误切。
   - 正文偶现的同名词不新建边界（解决「串内容」）。
   - 每个边界会打印来源**证据**（`标题` / `页码重置(上一文档末页→本页第1页)`），便于审阅切分是否合理。
4. **切片与命名**
   - 以边界页为起点切片；竖版页**矢量复制**（无损），旋转页栅格化转正（消除横版）。
   - 文件名取标题（去非法字符、截断 40 字），重名自动加序号。
   - 输出 `manifest.csv`，字段：`index / title / pages / out`。

### 四类文档的处理规则（保证切出来都完整）

- **① 附件**：首行常含「附件」字样、标题在前几行。`extract_title` 优先识别「附件N：标题」行并将其作为独立文档起点（保留「附件」标记以避免与其它文档误合并）；仅正文里顺带提及的「详见附件：…」不会被误判为边界。同一附件跨多页（表头重复）会按标题相同自动续页合并。
- **② 表单**：标题在前几行、行尾命中 `FORM_KW`（表/单/书/名册/清单…）即单独切为一篇；含表格栅格的续页因无新标题，自动并入当前表单（不切断、不遗漏）。
- **③ 合集/分册封面**：单页大标题、整页极稀疏（纯标题页）且不含表格栅格的页，判定为封面（`page_is_cover`），**前向合并到其后续文档的前导页**，不单独成篇。具体文档标题页（含 `DOC_KW` 或表单词）与附件/表单页不判为封面——它们本就会作为文档起点、正文作为续页并入，已保证完整；只有「非具体文档的纯标题页 / 合集标记（汇编、部分、篇、卷、目录…）」才前向合并。合并前用各段**原始起点**预判定封面，避免级联误并。
- **④ 制度类大段文字**：标题命中 `DOC_KW`（制度/规定/办法/细则/条例/规程/规范/手册…）即作为文档起点；正文长、跨多页且无重复标题的续页自动并入，整篇完整切出。
- **⑤ 页码模式（通用强信号）与空白分页纸**：上述①②③④之外，`find_docs` 还会用**页码重置**作为跨文档类型的通用强信号（见边界定位）。另外，扫描分隔纸 / 双面扫描的空白背面（OCR 文本极短）**不单独成篇、归属其所在文档**；整段纯空白则跳过输出（不生成空文件）——均借鉴 unstaple 的「空白页附前一文档、无损拆分」原则。

用法：

```bash
python segment_redhead.py <pdf路径> [--out DIR]
# 默认 --out D:\Backup\RayChan\split_redhead
```

---

## 功能二：批量切分（`batch_redhead.py`）

对 `D:\Backup\RayChan` 下所有 PDF 逐个调用切分：

- 已生成 `manifest.csv` 的册子自动跳过（支持断点续跑）；
- `--force` 忽略已有结果、全量重切；
- 单册异常不中断整体流程。

```bash
python batch_redhead.py [--force] [--src DIR] [--out DIR]
```

---

## 功能三：Windows 图形界面（`gui.py`）

免第三方 GUI 依赖（tkinter，Python 官方 Windows 安装包自带）：

- **添加 PDF 文件…** 多选；**添加目录…** 自动纳入目录下所有 PDF 批量处理；
- 双进度条实时显示：**当前文件** OCR 页进度（done/total）+ **总体进度**（按文件数加权）；
- 文件列表实时状态（等待 / 处理中 / 跳过 / 完成 / 失败 / 已取消），完成时显示切出文档数；
- **停止** 按钮立即终止当前文件的 OCR 进程池，已完成的页缓存落盘、下次自动续跑；
- 已生成 `manifest.csv` 的册子默认跳过，勾选**强制重切已存在**可全量重切；
- **打开输出目录** 一键查看产物；segment 的逐页证据日志实时显示在日志区。

```bash
python gui.py
```

---

## 功能四：视觉红头封面检测（`detect_redhead.py`）

不依赖 OCR 全文，直接基于版面视觉检测「红头封面」：

- `detect_redhead_covers`：定位「顶部有公司名大字号行 + 其下方居中大字号标题行」的候选页，返回 `(页码, 标题包围盒)`。
- `ocr_title_crop`：裁剪候选标题区域并 OCR，返回标题文本。
- 用于快速找候选封面、验证切分边界、人工抽检。`__main__` 会打印每个 PDF 命中的候选页码。

```bash
python detect_redhead.py
```

---

## 功能五：离线 HTTP 接口（`server.py`）

把切分能力以本地 HTTP 接口暴露给其它应用，**全程离线**（OCR 用本地 Tesseract，无外网调用）。监听 `127.0.0.1`，不对外开放。

启动：

```bash
pip install fastapi uvicorn python-multipart
python server.py            # http://127.0.0.1:8000
# 或: uvicorn server:app --host 127.0.0.1 --port 8000
```

端点：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查，返回 `{"status":"ok","offline":true,"tesseract":路径}` |
| POST | `/split` | body `{pdf_path, out_dir?}`，按本地路径切分 |
| POST | `/split/upload` | multipart 上传 PDF 切分（字段 `file`，可选 `out_dir`） |
| GET | `/download?path=绝对路径` | 取回产物（仅限本次输出根内，防目录穿越） |

### curl 示例

```bash
# 健康检查
curl http://127.0.0.1:8000/health

# 按本地路径切分
curl -X POST http://127.0.0.1:8000/split \
  -H "Content-Type: application/json" \
  -d "{\"pdf_path\":\"D:/Backup/RayChan/册1.pdf\"}"

# 上传文件切分
curl -X POST http://127.0.0.1:8000/split/upload -F "file=@册1.pdf"

# 下载某个切分产物（path 取上一步 manifest 里的 out 字段）
curl "http://127.0.0.1:8000/download?path=D:/Backup/RayChan/split_redhead/册1/标题.pdf" -o 标题.pdf
```

### Python 示例（requests）

```python
import requests

# 按路径切分
r = requests.post("http://127.0.0.1:8000/split",
                  json={"pdf_path": r"D:/Backup/RayChan/册1.pdf"})
print(r.json())          # {'pdf':..., 'out_dir':..., 'count':N, 'manifest':[...]}
for item in r.json()["manifest"]:
    print(item["title"], item["pages"], item["out"])

# 上传切分
with open("册1.pdf", "rb") as f:
    r = requests.post("http://127.0.0.1:8000/split/upload", files={"file": f})
print(r.json()["count"])
```

返回结构：`{pdf, out_dir, count, manifest}`，`manifest` 为列表，每项 `{index, title, pages, out}`，`out` 为产物绝对路径。

> 大册 OCR 耗时较长（几十~数百页可能数分钟），调用方请设置足够超时（如 `requests.post(..., timeout=300)`）。
> 如需局域网访问，请自行用反向代理并加鉴权，勿直接暴露本服务。

---

## 可调参数（`segment_redhead.py` 顶部）

| 参数 | 作用 |
|---|---|
| `DOC_KW` / `FORM_KW` | 文档型 / 表单型标题关键词。某类文种漏切时在此补充（表单型需以关键词结尾） |
| `EXCLUDE_PREFIX` / `EXCLUDE_SUBSTR` | 标题行排除词（页脚 / 正文 / 序号 / 括号开头）；注意「附件」虽在其中，但附件标题由 `find_attachment_title` 优先识别，不会被屏蔽 |
| `START_OK` | 文档型标题允许的起始词 |
| `COVER_PAGE_MAX` / `COVER_BODY_MAX` | 封面判定阈值：整页中文字数上限 / 标题行之后正文字数上限（极稀疏纯标题页才判为封面） |
| `COVER_MARKERS` | 合集/分册封面标记词（汇编/部分/篇/卷/目录/总册/集），命中即判封面 |
| `PN_RE_ZH` / `PN_RE_EN` | 页码正则：识别中文「第X页共Y页」与英文「Page X of Y」，用于页码重置信号（见边界定位） |
| `TESS_LANG` / `ZOOM` / `tesseract_cmd` | OCR 语言、渲染倍率、Tesseract 可执行文件路径 |
| `EXPECT_TOKENS` | 判向用的领域强特征词（避免用易在乱码中误中的单字） |

---

## 产物说明

- 输出：`D:\Backup\RayChan\split_redhead\<册名>\<标题>.pdf` + `manifest.csv`（每册一份索引）。
- OCR 缓存：`<pdf>.ocr.json` / `<pdf>.osd.json` 位于源 PDF 同目录，重跑复用、可续跑。
- 表单类（标题以 表/单/书/名册/清单… 结尾）默认会被识别为独立文档切出；若不希望切表单，可从 `FORM_KW` 移除对应词。

---

## 已知限制

- 基于 Tesseract 中文 OCR，偶有错字可能导致个别标题/边界偏差（如「文件处理笺」乱码页）。
- 文档型标题依赖 `DOC_KW` 命中；非常规文种若未命中关键词，可能未被识别为边界。
- 文件名仅为装订册名，切分粒度以 PDF 内部红头文档为准。

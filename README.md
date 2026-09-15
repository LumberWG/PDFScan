# PDFScan — 批量扫描 PDF 的文档级自动切分工具

把一册批量扫描或合成的 PDF（内含多份彼此独立的文档）自动识别每份文档的起始页，切分为独立 PDF，并以各文档标题命名。适用于把会议材料、公文汇编、档案合订本、合同集等「一册多文」的 PDF 拆成可单独检索、归档的文件。

> **关键认知**：PDF 文件名通常只是**装订册名**（例如「3-1 某材料汇编」），并不代表内部文档。真正用于切分的粒度是 PDF 内部**每一份带独立标题的文档**——每份文档有自己的标题行与机构落款，文档之间由封面、分页纸或空白页隔开。切分以文档边界为准，而非文件名。

---

## 环境要求

- Python 3.12（Windows arm64 实测通过）
- Tesseract OCR v5.x：安装到 `C:\Program Files\Tesseract-OCR\tesseract.exe`，中文包 `chi_sim.traineddata` 置于其 `tessdata\`
- Python 依赖：`pymupdf`(fitz)、`pytesseract`、`Pillow`、`numpy`；运行 `server.py` 还需 `fastapi`、`uvicorn`、`python-multipart`
- **OCR 引擎（可切换，全程离线）**：默认使用 **RapidOCR**（`rapidocr-onnxruntime`，基于 ONNX Runtime 的 PP-OCR 模型，完全离线、中文识别效果与速度均优于 Tesseract）。由于 RapidOCR 依赖的 opencv / onnxruntime 等仅在 **x86-64** 平台有预编译轮子，本工具**必须在 x86-64 版 Python 下运行**才能启用 RapidOCR（见下「运行环境与 OCR 引擎」）。未安装 RapidOCR 时自动回退 Tesseract；也可设环境变量 `PDFSCAN_OCR_BACKEND=tesseract` 强制走 Tesseract。

---

## 目录结构

```
PDFScan/
├── segment_redhead.py   # 核心：单册 PDF 的文档级切分
├── batch_redhead.py     # 批量：对指定目录下所有 PDF 跑切分
├── gui.py               # Windows 图形界面：选文件/目录切分 + 实时进度
├── detect_redhead.py    # 视觉法：检测封面候选页 + 裁剪 OCR 标题
├── server.py            # 离线 HTTP 接口（FastAPI，对外提供切分能力）
└── README.md
```
> 注：脚本文件名沿用早期命名（`redhead` 为历史用语，指「带机构抬头的文档」），功能本身已与具体业务领域无关。

---

## 运行环境与 OCR 引擎（RapidOCR）

### 为什么用 x86-64 Python
本工具默认 OCR 引擎为 **RapidOCR**（ONNX Runtime + PP-OCR 模型，离线、中文更准更快）。但其依赖的 opencv / onnxruntime 在 Windows **arm64** 上无预编译轮子；而在 arm64 Windows 上运行 **x86-64 版 Python** 时，这些包都有 x86_64 轮子，可直接 `pip install`。因此推荐用 x86-64 版 Python 运行本项目（arm64 Windows 原生支持 x86 仿真）。当前若仍用 arm64 Python，则会因装不上 RapidOCR 而自动回退到 Tesseract。

### 切换到 x86-64 Python 的步骤
1. 到 python.org 下载 **Windows installer (64-bit)** 版的 Python 3.12（注意：**不要**选 "arm64" 版本）。
2. 用该 Python 建虚拟环境并安装依赖：
   ```bash
   pip install pymupdf pytesseract Pillow numpy rapidocr-onnxruntime fastapi uvicorn python-multipart
   ```
3. 之后所有命令（`python gui.py` / `server.py` / `segment_redhead.py`）都用这个 x86-64 的 `python` 运行即可；RapidOCR 自带的 PP-OCR 模型随包安装（约 10–30MB，离线可用），无需额外下载。
4. 若想强制使用 Tesseract（例如未装 RapidOCR），保持系统装有 Tesseract 并设 `PDFSCAN_OCR_BACKEND=tesseract`。

---

## 快速开始

```bash
git clone https://github.com/LumberWG/PDFScan.git
cd PDFScan

# 1) 安装 Python 依赖
pip install pymupdf pytesseract Pillow numpy fastapi uvicorn python-multipart

# 2) Tesseract（二选一）
#    A. 系统安装：Tesseract v5.x 装到 C:\Program Files\Tesseract-OCR，中文包 chi_sim 放其 tessdata\
#    B. 便携捆绑(免安装)：把已装目录整目录复制到 vendor\tesseract\，脚本自动优先使用
#       New-Item -ItemType Directory -Force -Path vendor\tesseract
#       Copy-Item "C:\Program Files\Tesseract-OCR\*" vendor\tesseract\ -Recurse

# 3) 运行
python gui.py                  # Windows 图形界面（选文件/目录 + 实时进度）
python server.py               # 离线 HTTP 接口 (http://127.0.0.1:8000)
python segment_redhead.py <pdf>  # 或命令行直接切分
```

HTTP 接口端点：`/health`、`/split`、`/split/upload`、`/download`（详见 `server.py`）。

产物输出到 `<输出目录>/<册名>/`，每册一份 `manifest.csv`。输出目录默认在各脚本顶部常量中设定，可通过 `--out` 参数覆盖。

---

## 功能一：文档级切分（`segment_redhead.py`）

对**单册** PDF 执行完整切分流程：

1. **逐页 OCR（三级降本）** — `load_ocr`
   - **① 内嵌文本层优先**：PDF 自带中文文本层（≥10 个汉字）则直接采用，**完全跳过 OCR**（耗时接近 0）。
   - **② 空白页快筛**：低分辨率渲染算暗像素占比，几乎全白的页不再 OCR。
   - **③ 条带 OCR**：只渲染「页顶区域（机构落款 + 文档标题）+ 页脚区域（页码）」拼接成灰度图识别；读不出有效文字才回退更大范围并按 0/90/180/270 判向。
   - 并行进程数 = `min(CPU 核数, 8)`。
   - **注意**：判向旋转只作用于送进 OCR 的内存图像，绝不会写到 PDF 上——本软件只做切分，不改变 PDF 实际版面。
   - 结果缓存为 `<pdf>.ocr.json`（文本）与 `<pdf>.osd.json`（旋转角），可断点续跑、重跑复用。
2. **标题提取** — `extract_title`
   - 在「机构落款之后的前几行」中找文档标题行。判定规则：
     - 文档型：含文种关键词（如 纪要/通知/报告/通报/决定/决议/批复/办法/规定/细则/方案…），且以起始词开头 / 以关键词结尾 / 关键词在末几字；长度适中、无句中标点。
     - 表单型：以 表/单/书/名册/清单/记录… 结尾。
   - 通过大量排除词拦截页脚、正文、序号、括号开头等误报。
3. **边界定位** — `find_docs`（思路借鉴 [unstaple](https://github.com/BenMalaga/unstaple) 的相邻页决策）
   - **标题行法**：仅「含标题行」的页才可能是新文档起点；若其标题与上一边界标题相同 / 高度相似 / 为其截断子串，则视为同一文档续页（解决切得太碎——表单表头每页重复、长文档跨页）。
   - **页码重置法**：自动识别「第X页共Y页」「Page X of Y」；当某页是文档末页(当前页号==总页数)且下一页回到第1页，下一页**强制**为新文档起点（非对称优先，覆盖标题相似的误合并）。这补强了「无标准标题的新文档起点」检测。
   - 正文偶现的同名词不新建边界（解决串内容）。
   - 每个边界会打印来源**证据**（`标题` / `页码重置(上一文档末页→本页第1页)`），便于审阅切分是否合理。
4. **切片与命名**
   - 以边界页为起点切片；所有页**矢量复制**（无损、文字可选中），输出保持源页原始方向（含 `/Rotate` 属性），不按判向转正——侧躺扫描的页保持原状。保存后用 `check_layout_preserved` 逐页校验产物与源页旋转角/尺寸一致，不一致则告警（防止回归）。
   - 文件名取标题（去非法字符、截断），重名自动加序号。
   - 输出 `manifest.csv`，字段：`index / title / pages / out`。

### 几类常见文档的处理规则

- **附件**：首行常含「附件」字样、标题在前几行。优先识别「附件N：标题」行并作为独立文档起点（保留「附件」标记避免误合并）；仅正文里顺带提及的「详见附件：…」不会被误判。同一附件跨多页（表头重复）按标题相同自动续页合并。
- **表单**：标题在前几行、行尾命中表单关键词即单独切为一篇；含表格栅格的续页因无新标题，自动并入当前表单（不切断、不遗漏）。
- **封面 / 分册标记**：单页大标题、整页极稀疏（纯标题页）且不含表格栅格的页，判为封面，前向合并到其后续文档的前导页，不单独成篇。只有「非具体文档的纯标题页 / 分册标记（汇编、部分、篇、卷、目录…）」才前向合并；含具体文种关键词或表单词的文档标题页本就会作为文档起点。
- **正文长文档**：标题命中文种关键词即作为文档起点；正文长、跨多页且无重复标题的续页自动并入，整篇完整切出。
- **页码模式与空白分页**：除以上几类外，还会用页码重置作为跨文档类型的通用强信号。扫描分隔纸 / 双面扫描的空白背面（OCR 文本极短）不单独成篇、归属其所在文档；整段纯空白则跳过输出（不生成空文件）。

用法：

```bash
python segment_redhead.py <pdf路径> [--out 输出目录]
```

---

## 提速策略（实测 ~1.5x）

瓶颈几乎全在 Tesseract 单页识别。已按「少识别、别白识别、并行吃满核」三条线优化；若 PDF **自带文本层则接近 0 秒**。

| 手段 | 做法 | 收益 |
| --- | --- | --- |
| 免 OCR | PDF 内嵌文本层够用（≥10 汉字）就直接用 | 有文本层时 ~0ms/页 |
| 跳过空白页 | 低分辨率渲染算暗像素占比，极稀疏即跳过 | 空白页越多收益越大 |
| 缩小识别面积 | 只取「页顶 + 页脚」条带 | 像素降约 40%，耗时 -27% |
| 灰度 + `--psm 6` | 省去色彩转换与版面分析 | 约 -13% |
| 吃满核 | 进程池 `min(CPU 核数, 8)` | 约 -18% |
| 缓存 | `<pdf>.ocr.json` 断点续跑、重跑复用 | 重复运行几乎零成本 |

---

## 功能二：批量切分（`batch_redhead.py`）

对指定目录下所有 PDF 逐个调用切分：

- 已生成 `manifest.csv` 的册子自动跳过（支持断点续跑）；
- `--force` 忽略已有结果、全量重切；
- 单册异常不中断整体流程。

```bash
python batch_redhead.py [--force] [--src 源目录] [--out 输出目录]
```

---

## 功能三：Windows 图形界面（`gui.py`）

免第三方 GUI 依赖（tkinter，Python 官方 Windows 安装包自带）：

- **添加 PDF 文件…** 多选；**添加目录…** 自动纳入目录下所有 PDF 批量处理；
- 双进度条实时显示：**当前文件** OCR 页进度 + **总体进度**（按文件数加权）；
- 文件列表实时状态（等待 / 处理中 / 跳过 / 完成 / 失败 / 已取消），完成时显示切出文档数；
- **自动重切被改版面旧产物**（默认勾选）：已切册子按 manifest 与源 PDF 逐页比对方向，发现「页面被旋转过」即自动重切，源 PDF 本身就是横版页属正常不会误判；
- **检查状态** 按钮只扫描不处理：标出每册 未处理 / 已切(正常) / 已切·版面被改N个；
- **停止** 按钮立即终止当前文件的 OCR 进程池，已完成页缓存落盘、下次续跑；
- 已生成 `manifest.csv` 的册子默认跳过，勾选**强制重切已存在**可全量重切；
- **打开输出目录** 一键查看产物。

```bash
python gui.py
```

---

## 功能四：视觉封面检测（`detect_redhead.py`）

不依赖 OCR 全文，直接基于版面视觉检测「封面候选页」：

- `detect_redhead_covers`：定位「顶部有机构名大字号行 + 其下方居中大字号标题行」的候选页，返回 `(页码, 标题包围盒)`。
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
| GET | `/health` | 健康检查，返回状态与 Tesseract 路径 |
| POST | `/split` | body `{pdf_path, out_dir?}`，按本地路径切分 |
| POST | `/split/upload` | multipart 上传 PDF 切分（字段 `file`，可选 `out_dir`） |
| GET | `/download?path=绝对路径` | 取回产物（仅限本次输出根内，防目录穿越） |

### 调用示例（curl）

```bash
curl http://127.0.0.1:8000/health

# 按本地路径切分
curl -X POST http://127.0.0.1:8000/split \
  -H "Content-Type: application/json" \
  -d "{\"pdf_path\":\"D:/docs/册1.pdf\"}"

# 上传文件切分
curl -X POST http://127.0.0.1:8000/split/upload -F "file=@册1.pdf"

# 下载某个切分产物（path 取上一步 manifest 里的 out 字段）
curl "http://127.0.0.1:8000/download?path=D:/output/册1/标题.pdf" -o 标题.pdf
```

返回结构：`{pdf, out_dir, count, manifest}`，`manifest` 为列表，每项 `{index, title, pages, out}`，`out` 为产物绝对路径。

> 大册 OCR 耗时较长（几十~数百页可能数分钟），调用方请设置足够超时（如 `requests.post(..., timeout=300)`）。
> 如需局域网访问，请自行用反向代理并加鉴权，勿直接暴露本服务。

---

## 可调参数与领域词典（`segment_redhead.py` 顶部）

本工具内置了一套**示例领域词典**（默认偏向通用公文 / 表单），可按你的业务领域自定义：

| 参数 | 作用 |
|---|---|
| `DOC_KW` / `FORM_KW` | 文档型 / 表单型标题关键词。某类文种漏切时在此补充（表单型需以关键词结尾） |
| `EXCLUDE_PREFIX` / `EXCLUDE_SUBSTR` | 标题行排除词（页脚 / 正文 / 序号 / 括号开头）；注意「附件」虽在其中，但附件标题由 `find_attachment_title` 优先识别，不会被屏蔽 |
| `START_OK` | 文档型标题允许的起始词 |
| `COVER_PAGE_MAX` / `COVER_BODY_MAX` | 封面判定阈值（仅在无文档关键词的纯文本回退路径生效）：整页中文字数上限 / 标题行之后正文字数上限 |
| `COVER_MARKERS` | 分册封面标记词（汇编/部分/篇/卷/目录/总册/集），命中即判封面 |
| `SPARSE_DARK_RATIO` / `COVER_MAX_PAGES` | 封面判定首选判据：整页暗像素占比上限；以及允许作为封面前向合并的最大页数(≤2，防误吞文档) |
| `PN_RE_ZH` / `PN_RE_EN` | 页码正则：识别中文「第X页共Y页」与英文「Page X of Y」，用于页码重置信号 |
| `TESS_LANG` / `OCR_ZOOM` / `tesseract_cmd` | OCR 语言、渲染倍率、Tesseract 可执行文件路径 |
| `OCR_BACKEND` | OCR 后端选择：环境变量 `PDFSCAN_OCR_BACKEND`，`rapidocr`(默认) / `tesseract`(强制) |
| `TOP_FAST_RATIO` / `FOOT_RATIO` / `TOP_RATIO` | 取图范围：首选「页顶 + 页脚」条带；读不出文字时回退更大范围。标题整体偏下(封面式排版)可把 `TOP_FAST_RATIO` 调大 |
| `TEXT_LAYER_MIN_CN` / `BLANK_DARK_RATIO` | 免 OCR 文本层的中文下限(0 可禁用)；空白页判定的暗像素占比上限 |
| `OCR_CONFIG_FAST` / `BAND_GAP` | Tesseract 参数(默认 `--psm 6`)；条带拼接时的白边像素 |
| `EXPECT_TOKENS` | 判向用的领域强特征词（避免用易在乱码中误中的单字） |
| `STD_COMPANY` / `STD_TYPES` / `OCR_FIX` | 机构名归一、文种标准词与错字校正映射（可按领域替换） |

> 要彻底适配某一行业（如合同、试卷、票据、档案），主要就是调整以上词典常量。后续版本计划将其**外置为配置文件**，做到零代码改动即可切换领域。

---

## 产物说明

- 输出：`<输出目录>/<册名>/<标题>.pdf` + `manifest.csv`（每册一份索引）。
- OCR 缓存：`<pdf>.ocr.json` / `<pdf>.osd.json` 位于源 PDF 同目录，重跑复用、可续跑。
- 表单类（标题以 表/单/书/名册/清单… 结尾）默认会被识别为独立文档切出；若不希望切表单，可从 `FORM_KW` 移除对应词。

---

## 已知限制

- 基于 Tesseract 中文 OCR，偶发错字可能导致个别标题/边界偏差。
- 文档型标题依赖关键词命中；非常规文种若未命中关键词，可能未被识别为边界。
- 文件名仅为装订册名，切分粒度以 PDF 内部独立文档为准。
- 侧躺扫描的页（内容横躺但页面为竖版）**保持源页方向输出、不自动转正**：OCR 判向仍用于标题/边界识别（`.osd.json` 缓存），但切片时不旋转页面，此类页内容在产物中仍是侧躺的。

---

## 后续通用化方向

当前文档已去除具体业务领域内容；要让软件**完全与领域解耦**，还建议：

1. **词典外置为配置文件**（JSON / YAML）：把 `DOC_KW`/`FORM_KW`/`STD_COMPANY`/`STD_TYPES`/`OCR_FIX`/`EXPECT_TOKENS` 等抽到独立配置，零代码切换领域。
2. **默认输出路径泛化**：将各脚本顶部的硬编码输出目录改为相对路径或统一常量，避免绑定特定用户目录。
3. **脚本重命名**：`*_redhead` 命名沿用早期用语，可统一重命名为 `*_doc` 等通用名（需同步更新 import 引用）。

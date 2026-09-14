"""批量红头切分 D:\\Backup\\RayChan 下所有 PDF。已生成 manifest 的跳过(除非 --force)，异常不中断、缓存可续跑。
用法: python batch_redhead.py [--force] [--src DIR] [--out DIR]
"""
import os, re, glob, argparse
from segment_redhead import segment

SRC = r"D:\Backup\RayChan"
OUT = r"D:\Backup\RayChan\split_redhead"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略已有 manifest，全量重切")
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    pdfs = sorted(glob.glob(os.path.join(args.src, "*.pdf")))
    print(f"共发现 {len(pdfs)} 个 PDF" + (" (--force 全量重切)" if args.force else ""), flush=True)
    for pdf in pdfs:
        base = re.sub(r"\.pdf$", "", os.path.basename(pdf), flags=re.I)
        man = os.path.join(args.out, base, "manifest.csv")
        if os.path.exists(man) and not args.force:
            print(f"[SKIP] 已处理: {base}", flush=True)
            continue
        print(f"[START] {base}", flush=True)
        try:
            segment(pdf, args.out)
        except Exception as e:
            print(f"[ERROR] {base}: {e}", flush=True)
    print("[DONE] 全部处理完毕", flush=True)


if __name__ == "__main__":
    main()

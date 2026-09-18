"""Fetch the BIRD benchmark into data/bird/.

Why Mini-Dev rather than the full dev set: Mini-Dev is a published, fixed
500-question subset that other people report numbers against, so a score on it
is comparable. Sampling the 1,534-question dev set myself with a seed would be
reproducible but not comparable -- nobody else has my sample.

Two sources are needed, because they live in different places:

  questions  HuggingFace, ~280 KB. Ships in three dialects; the SQLite one is
             the original, the Postgres and MySQL ones were transpiled with
             sqlglot (the same library this project validates with).
  databases  The official dev.zip, ~346 MB. Mini-Dev draws on the eleven dev
             databases and there is no smaller bundle of just those.

Both steps skip work that is already done, so re-running is cheap.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BIRD_DIR = REPO_ROOT / "data" / "bird"

DEV_ZIP_URL = "https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip"
HF_QUESTIONS = (
    "https://huggingface.co/datasets/birdsql/bird_mini_dev/resolve/main/data/"
    "mini_dev_{dialect}-00000-of-00001.json"
)
DIALECTS = {"sqlite": "sqlite", "postgres": "pg", "mysql": "mysql"}


def _download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  have {dest.name} ({dest.stat().st_size/1e6:.1f} MB)")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")

    def progress(block: int, size: int, total: int) -> None:
        if total > 0 and block % 200 == 0:
            pct = min(100.0, 100.0 * block * size / total)
            print(f"\r    {pct:5.1f}%  of {total/1e6:.0f} MB", end="", flush=True)

    urllib.request.urlretrieve(url, tmp, reporthook=progress)
    print()
    tmp.rename(dest)
    return dest


def _unpack_databases(zip_path: Path, out_dir: Path) -> int:
    """Extract every .sqlite in the archive, including from nested zips.

    The dev archive nests dev_databases.zip inside dev.zip, and the exact
    directory names have changed between releases, so this walks the archive
    rather than hard-coding a path that will rot.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    found = 0
    work = REPO_ROOT / "data" / "bird" / "_unpack"
    work.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        inner = [n for n in zf.namelist() if n.endswith(".zip")]
        members = [n for n in zf.namelist() if n.endswith(".sqlite")]
        for name in members:
            db_id = Path(name).stem
            target = out_dir / db_id / f"{db_id}.sqlite"
            if target.exists():
                found += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            found += 1
        for name in inner:
            extracted = zf.extract(name, work)
            found += _unpack_databases(Path(extracted), out_dir)
    shutil.rmtree(work, ignore_errors=True)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dialect", choices=sorted(DIALECTS), default="sqlite")
    ap.add_argument("--questions-only", action="store_true",
                    help="skip the 346 MB database download")
    args = ap.parse_args()

    BIRD_DIR.mkdir(parents=True, exist_ok=True)
    print("questions:")
    qdir = BIRD_DIR / "questions"
    qpath = qdir / f"mini_dev_{args.dialect}.json"
    _download(HF_QUESTIONS.format(dialect=DIALECTS[args.dialect]), qpath)
    questions = json.loads(qpath.read_text())
    dbs = sorted({q["db_id"] for q in questions})
    print(f"  {len(questions)} questions over {len(dbs)} databases")

    if args.questions_only:
        print("\nskipping databases (--questions-only)")
        return 0

    print("\ndatabases:")
    zip_path = BIRD_DIR / "dev.zip"
    _download(DEV_ZIP_URL, zip_path)
    count = _unpack_databases(zip_path, BIRD_DIR / "databases")
    print(f"  unpacked {count} sqlite databases")

    missing = [d for d in dbs if not (BIRD_DIR / "databases" / d / f"{d}.sqlite").exists()]
    if missing:
        print(f"\nWARNING: {len(missing)} databases referenced by questions are "
              f"missing: {', '.join(missing)}", file=sys.stderr)
        return 1
    print(f"\nready: {BIRD_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

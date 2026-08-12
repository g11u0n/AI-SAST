#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, shutil, sys
from pathlib import Path
PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path: sys.path.insert(0,str(PROJECT_ROOT))
from src.runtime import AnalysisRunner  # noqa: E402

def find_git(explicit:Path|None)->Path:
    if explicit is not None:
        candidate=explicit.resolve()
        if not candidate.is_file(): raise ValueError(f"Git executable not found: {candidate}")
        return candidate
    found=shutil.which("git")
    if found: return Path(found).resolve()
    raise ValueError("Git executable not found on PATH")

def main()->int:
    p=argparse.ArgumentParser()
    p.add_argument("--root",type=Path,default=PROJECT_ROOT)
    p.add_argument("--repo",type=Path,default=Path(".target-src/userland"))
    p.add_argument("--git",type=Path)
    p.add_argument("--mode",choices=["selected","priority","all"],default="selected")
    p.add_argument("--limit",type=int)
    p.add_argument("--output",type=Path,default=Path("results_v3"))
    p.add_argument("--dry-run",action="store_true")
    p.add_argument("--resume",action="store_true",help="Skip completed Batch checkpoints in the output directory")
    a=p.parse_args()
    try:
        if a.limit is not None and a.limit<=0: raise ValueError("--limit must be positive")
        root=a.root.resolve()
        repo=(a.repo if a.repo.is_absolute() else root/a.repo).resolve()
        out=(a.output if a.output.is_absolute() else root/a.output).resolve()
        runner=AnalysisRunner(root=root,repository=repo,git_executable=find_git(a.git))
        result=runner.dry_run(mode=a.mode,limit=a.limit) if a.dry_run else runner.run(
            mode=a.mode,limit=a.limit,output_dir=out,resume=a.resume
        )
        print(json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True))
        return 0
    except Exception as exc:
        print(f"AI-SAST runtime failed: {type(exc).__name__}: {exc}",file=sys.stderr)
        return 1

if __name__=="__main__":
    raise SystemExit(main())

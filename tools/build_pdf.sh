#!/usr/bin/env bash
# 把 README + SPEC + DD-01..07 + 审计 合成一本 PDF（ctexart + xelatex）
# 用法：bash tools/build_pdf.sh   （在项目根目录运行）
# 输出：pdf/stata-agent-design-<YYYY-MM-DD>.pdf
set -euo pipefail
cd "$(dirname "$0")/.."   # 项目根

OUT_DIR="pdf"; BUILD_DIR="build"
mkdir -p "$OUT_DIR" "$BUILD_DIR"

STAMP=$(date +%Y-%m-%d)
OUT="$OUT_DIR/stata-agent-design-$STAMP.pdf"

FILES=(
  README.md
  design/SPEC.md
  design/dd-01-domain-events.md
  design/dd-02-phase-machine-harness.md
  design/dd-03-context-memory.md
  design/dd-04-tool-permission.md
  design/dd-05-writer-validator.md
  design/dd-06-eval.md
  design/dd-07-rag-skills.md
  design/audit-stata-practice.md
)

{
cat <<'YAML'
---
title: "实证研究 Agent · 设计文档集"
subtitle: "SPEC v0.5 · 详细设计 DD-01—07 · Stata 实证审计"
author: "Stata Agent 项目"
date: "2026-09-07"
lang: zh
---
YAML
for f in "${FILES[@]}"; do
  printf '\n\n\\newpage\n\n'  # 各章独立起页
  cat "$f"
done
} > "$BUILD_DIR/combined.md"

# 预处理：把正文里 emoji 记号/数学减号换成 LaTeX 宏（跳过代码块），避免缺字形
python - "$BUILD_DIR/combined.md" > "$BUILD_DIR/combined_final.md" <<'PY'
import sys
sys.stdout.reconfigure(encoding="utf-8")
src = open(sys.argv[1], encoding="utf-8").read()
out, fence = [], False
for line in src.split("\n"):
    if line.lstrip().startswith("```"):
        fence = not fence; out.append(line); continue
    if not fence:
        line = line.replace("✅", r" \yesmark{} ").replace("✔", r" \yesmark{} ").replace("🟡", r" \warnmark{} ")
        line = line.replace("−", "-")   # 数学减号 → 连字符
    out.append(line)
sys.stdout.write("\n".join(out))
PY

pandoc "$BUILD_DIR/combined_final.md" \
  -o "$OUT" \
  --pdf-engine=xelatex \
  -V documentclass=ctexbook \
  -V geometry:"margin=2.2cm,top=2.3cm,bottom=2.4cm" \
  --top-level-division=chapter \
  --toc --toc-depth=2 \
  -H tools/preamble.tex \
  -M lang=zh-CN

echo "OK -> $OUT ($(du -h "$OUT" | cut -f1))"

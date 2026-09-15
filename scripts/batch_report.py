"""results.csv 를 집계해 batch/summary.md 와 batch/fixture_candidates.md 를 만든다.

사용: ``uv run python scripts/batch_run.py`` 후 ``uv run python scripts/batch_report.py``
"""

from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.batch_classify import CODE_DESCRIPTIONS, is_normal  # noqa: E402

RESULTS_CSV = Path("batch/results.csv")
SUMMARY_MD = Path("batch/summary.md")
CANDIDATES_MD = Path("batch/fixture_candidates.md")


def _codes(row: dict) -> list[str]:
    return [c for c in (row.get("경고요약") or "").split("|") if c]


def _rate(rows: list[dict]) -> str:
    if not rows:
        return "-"
    ok = sum(1 for r in rows if is_normal(r["성공여부"] == "성공", _codes(r)))
    return f"{ok}/{len(rows)} ({ok / len(rows) * 100:.0f}%)"


def main() -> None:
    rows = list(csv.DictReader(RESULTS_CSV.open(encoding="utf-8-sig")))
    if not rows:
        sys.exit("results.csv 가 비어 있습니다. batch_run.py 를 먼저 실행하세요.")

    lines: list[str] = ["# 배치 검증 요약", ""]

    # 1. 전체 + 유형×corp_cls 정상률
    lines += [f"## 1. 전체: {len(rows)}건, 정상률 {_rate(rows)}", "",
              "| 유형 | corp_cls | 건수 | 정상률 |", "|---|---|---|---|"]
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["유형"], r["corp_cls"]), []).append(r)
    for (ty, cls), g in sorted(groups.items()):
        lines.append(f"| {ty} | {cls} | {len(g)} | {_rate(g)} |")
    lines.append("")

    # 2. 코드별 발생 건수 + 예시
    counts: dict[str, list[dict]] = {}
    for r in rows:
        for c in _codes(r):
            counts.setdefault(c, []).append(r)
    lines += ["## 2. 코드별 발생 건수", "", "| 코드 | 건수 | 설명 | 예시 (rcpNo 회사 유형) |", "|---|---|---|---|"]
    for code, g in sorted(counts.items(), key=lambda kv: -len(kv[1])):
        ex = "; ".join(f"{r['rcpNo']} {r['corp_name']} {r['유형']}" for r in g[:3])
        lines.append(f"| {code} | {len(g)} | {CODE_DESCRIPTIONS.get(code, '')} | {ex} |")
    lines.append("")

    # 3. 주석 source 분포
    src_total: dict[str, int] = {}
    for r in rows:
        for part in (r.get("주석source분포") or "").split(";"):
            if "=" in part:
                k, v = part.split("=")
                src_total[k] = src_total.get(k, 0) + int(v)
    total_notes = sum(src_total.values()) or 1
    lines += ["## 3. 주석 source 분포 (전체 주석 기준)", ""]
    for k, v in sorted(src_total.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {k}: {v}개 ({v / total_notes * 100:.1f}%)")
    lines.append("")

    # 4. 소요시간
    times = sorted((float(r["소요초"]), r) for r in rows if r.get("소요초"))
    if times:
        vals = [t for t, _ in times]
        lines += ["## 4. 소요시간", "",
                  f"- 중앙값 {statistics.median(vals):.1f}초, 최대 {vals[-1]:.1f}초", "- 상위 3건:"]
        for t, r in times[-3:][::-1]:
            lines.append(f"  - {r['rcpNo']} {r['corp_name']} {r['유형']}: {t:.1f}초")
    lines.append("")

    # 5. fixture 후보: F_/N_/E_ 코드별 첫 발생 1건
    cand_lines = ["# fixture 후보 (코드별 첫 발생)", "",
                  "| 코드 | rcpNo | 회사 | 유형 | 경고요약 | 캐시 경로 |", "|---|---|---|---|---|---|"]
    seen_codes: set[str] = set()
    for r in rows:
        for c in _codes(r):
            if not c.startswith(("F_", "N_", "E_")) or c in seen_codes:
                continue
            seen_codes.add(c)
            cand_lines.append(
                f"| {c} | {r['rcpNo']} | {r['corp_name']} | {r['유형']} | {r['경고요약']} | batch/cache/{r['rcpNo']}/ |"
            )
    lines += [f"## 5. fixture 후보: {len(seen_codes)}개 코드 → batch/fixture_candidates.md", ""]

    SUMMARY_MD.write_text("\n".join(lines), encoding="utf-8")
    CANDIDATES_MD.write_text("\n".join(cand_lines), encoding="utf-8")
    print(f"{SUMMARY_MD} / {CANDIDATES_MD} 작성 완료 ({len(rows)}건)")


if __name__ == "__main__":
    main()

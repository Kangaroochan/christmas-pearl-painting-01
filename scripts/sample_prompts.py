#!/usr/bin/env python3
"""
按"插槽 + 素材池"规格批量抽取提示词，保证任意两条之间至少有 min_diff 处不同。

用法：
    python sample_prompts.py spec.json -o prompts.xlsx              # 不限条数，生成素材池能支持的最大数量
    python sample_prompts.py spec.json -o prompts.xlsx --count 300  # 指定条数，凑不够就报错
    python sample_prompts.py spec.json -o prompts.xlsx --limit 5000 # 调整不限条数时的安全上限（默认 2000）
    python sample_prompts.py spec.json -o prompts.xlsx --max-seconds 300  # 最长运行时间，默认 90 秒

"一处不同"的定义：一个维度取值不同。维度包括：
    每个全局插槽、每个内容类别（角色）抽到的内容、每个角色所在格子的位置插槽（机位/景别等）、宫格排列。
    按角色比较而不是按位置比较：两张图只是把同一组画面换了位置时，只有"排列"不同，算 1 处。

规格（JSON）字段：
    template        必填。用 {{名字}} 表示占位符。
    min_diff        任意两条至少几处不同，默认 10。
    count           可选。不填 = 生成素材池能支持的最大条数（受 --limit 安全上限约束）。
    seed            可选随机种子。
    slots           全局插槽，整张图一个取值：
                    {名字: {"pool": [...], "max_uses": 可选}}
    groups          位置可打乱的一组内容（如四宫格）：
                    [{"positions": ["格1", ...],          # 模板里的位置占位符
                      "roles": {角色: {"pool": [...]}},     # 数量必须等于 positions 数量
                      "shuffle": true,                     # 是否打乱角色所在位置
                      "avoid_identity": false,             # 打乱时不出现 roles 原来的顺序
                      "position_slots": {                  # 可选：每个位置各抽一次的插槽
                          "机位": {"pool": [...], "distinct": true}   # distinct=同一张图里各位置互不相同
                      }}]
                    模板里用 {{格1}} 引用该位置的内容，用 {{格1.机位}} 引用该位置的机位。
    conflicts       可选。不能同时出现的取值组合：
                    [{"维度A": "取值或取值列表", "维度B": "取值或取值列表"}, ...]
                    维度名可以是全局插槽名、位置名（如 "格1"）或 "格1.机位"。
                    一条规则里的所有条件同时满足时，该组合被丢弃。
"""
import argparse
import csv
import json
import math
import random
import re
import sys
import time
from collections import Counter

try:
    import numpy as np
except ImportError:  # 没有 numpy 也能跑，只是慢一些
    np = None

PH = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
DEFAULT_LIMIT = 2000


def fail(msg):
    sys.exit(f"[错误] {msg}")


def clean_pool(name, pool):
    pool = [str(p).strip() for p in pool]
    pool = [p for p in dict.fromkeys(pool) if p]
    if not pool:
        fail(f"「{name}」的素材池为空")
    return pool


class Spec:
    def __init__(self, raw, count, min_diff):
        if "template" not in raw:
            fail("规格缺少 template")
        self.template = raw["template"]
        self.count = count
        self.min_diff = min_diff
        self.slots = {}
        for name, cfg in raw.get("slots", {}).items():
            self.slots[name] = {"pool": clean_pool(name, cfg["pool"]),
                                "max_uses": cfg.get("max_uses")}
        self.groups = []
        for gi, g in enumerate(raw.get("groups", [])):
            positions, roles = g["positions"], g["roles"]
            if len(roles) != len(positions):
                fail(f"第 {gi+1} 个 group 的 roles 数量({len(roles)})"
                     f"和 positions 数量({len(positions)})不一致")
            pslots = {}
            for sname, cfg in g.get("position_slots", {}).items():
                pool = clean_pool(sname, cfg["pool"])
                distinct = cfg.get("distinct", True)
                if distinct and len(pool) < len(positions):
                    fail(f"位置插槽「{sname}」设了 distinct，但只有 {len(pool)} 项，"
                         f"不够分给 {len(positions)} 个位置")
                pslots[sname] = {"pool": pool, "distinct": distinct}
            self.groups.append({
                "positions": positions,
                "roles": {r: {"pool": clean_pool(r, c["pool"]), "max_uses": c.get("max_uses")}
                          for r, c in roles.items()},
                "shuffle": g.get("shuffle", True) and len(positions) > 1,
                "avoid_identity": g.get("avoid_identity", False),
                "position_slots": pslots,
            })
        self.conflicts = raw.get("conflicts", [])
        self._check_names()

    def _check_names(self):
        names = list(self.slots)
        for g in self.groups:
            names += list(g["roles"]) + list(g["positions"])
        dup = [n for n, c in Counter(names).items() if c > 1]
        if dup:
            fail(f"插槽/角色/位置名重复：{dup}")

    # 每个"差异维度"的名字，用于统计和输出。宫格按角色计，不按位置计
    def dim_names(self):
        names = list(self.slots)
        for gi, g in enumerate(self.groups):
            names += list(g["roles"])
            for r in g["roles"]:
                names += [f"{r}.{s}" for s in g["position_slots"]]
            if g["shuffle"]:
                names.append(f"排列{gi+1}")
        return names


def check_template(spec):
    used = set(PH.findall(spec.template))
    expected = set(spec.slots)
    for g in spec.groups:
        expected |= set(g["positions"])
        expected |= {f"{p}.{s}" for p in g["positions"] for s in g["position_slots"]}
    unknown = used - expected
    if unknown:
        fail(f"模板里有未定义的占位符：{sorted(unknown)}")
    missing = expected - used
    if missing:
        fail(f"这些维度定义了但模板里没用到，会造成假差异：{sorted(missing)}")


def check_conflicts(spec):
    valid = {n: set(c["pool"]) for n, c in spec.slots.items()}
    for g in spec.groups:
        content = set().union(*[set(r["pool"]) for r in g["roles"].values()])
        for p in g["positions"]:
            valid[p] = content
            for s, c in g["position_slots"].items():
                valid[f"{p}.{s}"] = set(c["pool"])
    for i, rule in enumerate(spec.conflicts, 1):
        for dim, vals in rule.items():
            if dim not in valid:
                fail(f"conflicts 第 {i} 条引用了不存在的维度「{dim}」")
            for v in ([vals] if isinstance(vals, str) else vals):
                if v not in valid[dim]:
                    fail(f"conflicts 第 {i} 条里「{dim}」没有取值「{v}」")


def caps(spec):
    """每个选项的使用上限。指定条数时按平均×1.5；不限条数时只做均衡，不设硬上限。"""
    def cap(pool_len, max_uses):
        if max_uses is not None:
            return max_uses
        if spec.count is None:
            return math.inf
        return math.ceil(math.ceil(spec.count / pool_len) * 1.5)

    result = {}
    for n, c in spec.slots.items():
        result[n] = cap(len(c["pool"]), c["max_uses"])
    for g in spec.groups:
        for r, c in g["roles"].items():
            result[r] = cap(len(c["pool"]), c["max_uses"])
    if spec.count is not None:
        for n, c in list(spec.slots.items()) + [(r, c) for g in spec.groups for r, c in g["roles"].items()]:
            if result[n] * len(c["pool"]) < spec.count:
                fail(f"「{n}」只有 {len(c['pool'])} 项、每项最多 {result[n]} 次，凑不够 {spec.count} 条")
    return result


def pick(pool, usage, cap, rng, exclude=()):
    avail = [p for p in pool if usage[p] < cap and p not in exclude]
    if not avail:
        return None
    low = min(usage[p] for p in avail)
    weights = [3 if usage[p] == low else 1 for p in avail]
    return rng.choices(avail, weights=weights, k=1)[0]


def draw(spec, usage, cap, rng):
    """抽一个候选组合，返回 (按维度排列的取值字典, 渲染用的取值字典)。
    维度字典按角色记录宫格内容和位置插槽；渲染字典按位置记录，用于模板和 conflicts。"""
    dims, values = {}, {}
    for n, c in spec.slots.items():
        v = pick(c["pool"], usage[n], cap[n], rng)
        if v is None:
            return None
        dims[n] = values[n] = v
    for gi, g in enumerate(spec.groups):
        for r, c in g["roles"].items():
            v = pick(c["pool"], usage[r], cap[r], rng)
            if v is None:
                return None
            dims[r] = v
        order = list(g["roles"])
        if g["shuffle"]:
            rng.shuffle(order)
            while g["avoid_identity"] and order == list(g["roles"]):
                rng.shuffle(order)
            dims[f"排列{gi+1}"] = "；".join(f"{p}={r}" for p, r in zip(g["positions"], order))
        for p, r in zip(g["positions"], order):
            values[p] = dims[r]
        for s, c in g["position_slots"].items():
            taken = []
            for p, r in zip(g["positions"], order):
                key = f"{p}.{s}"
                v = pick(c["pool"], usage[key], math.inf, rng,
                         exclude=taken if c["distinct"] else ())
                taken.append(v)
                values[key] = dims[f"{r}.{s}"] = v
    return dims, values


def violates(spec, values):
    for rule in spec.conflicts:
        if all(values.get(d) in ([v] if isinstance(v, str) else v) for d, v in rule.items()):
            return True
    return False


def generate(spec, rng, limit, tries, max_seconds):
    names = spec.dim_names()
    if len(names) < spec.min_diff:
        fail(f"只有 {len(names)} 个差异维度，不可能做到任意两条 {spec.min_diff} 处不同。"
             f"请拆出更多维度（建议至少 {spec.min_diff + 4} 个）")
    cap = caps(spec)
    usage = {n: Counter() for n in list(spec.slots) + [r for g in spec.groups for r in g["roles"]]}
    for g in spec.groups:
        for p in g["positions"]:
            for s in g["position_slots"]:
                usage[f"{p}.{s}"] = Counter()

    codes = {n: {} for n in names}  # 取值 → 整数编码，便于向量化比较
    target = spec.count if spec.count is not None else limit
    max_agree = len(names) - spec.min_diff
    matrix = np.zeros((target, len(names)), dtype=np.int32) if np else None
    rows, min_seen = [], math.inf
    deadline = time.monotonic() + max_seconds
    stop = "saturated"

    while len(rows) < target:
        if time.monotonic() > deadline:
            stop = "timeout"
            break
        if len(rows) and len(rows) % 1000 == 0 and rows[-1].get("_logged") is None:
            rows[-1]["_logged"] = True
            print(f"  ……已生成 {len(rows)} 条", flush=True)
        accepted = None
        for _ in range(tries):
            got = draw(spec, usage, cap, rng)
            if got is None:
                break
            dims, values = got
            if violates(spec, values):
                continue
            vec = [codes[n].setdefault(dims[n], len(codes[n])) for n in names]
            if rows:
                if np is not None:
                    agree = (matrix[:len(rows)] == np.array(vec, dtype=np.int32)).sum(axis=1)
                    worst = int(agree.max())
                else:
                    worst = max(sum(a == b for a, b in zip(r["vec"], vec)) for r in rows)
                if worst > max_agree:
                    continue
                min_seen = min(min_seen, len(names) - worst)
            accepted = (dims, values, vec)
            break
        if accepted is None:
            break
        dims, values, vec = accepted
        if np is not None:
            matrix[len(rows)] = vec
        for n in spec.slots:
            usage[n][dims[n]] += 1
        for g in spec.groups:
            for r in g["roles"]:
                usage[r][dims[r]] += 1
            for p in g["positions"]:
                for s in g["position_slots"]:
                    usage[f"{p}.{s}"][values[f"{p}.{s}"]] += 1
        rows.append({"dims": dims, "values": values, "vec": vec})
    if len(rows) >= target:
        stop = "target"
    return names, rows, usage, cap, (min_seen if len(rows) > 1 else None), stop


def render(spec, values):
    return PH.sub(lambda m: values[m.group(1).strip()], spec.template)


def write_output(path, spec, names, rows):
    header = ["编号", "提示词"] + names
    table = [[i, render(spec, r["values"])] + [r["dims"][n] for n in names]
             for i, r in enumerate(rows, 1)]
    if path.lower().endswith(".xlsx"):
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font
        except ImportError:
            path = path[:-5] + ".csv"
            print("[提醒] 没装 openpyxl，改为输出 CSV")
        else:
            wb = Workbook()
            ws = wb.active
            ws.title = "提示词"
            ws.append(header)
            for row in table:
                ws.append(row)
            for c in ws[1]:
                c.font = Font(bold=True)
            ws.column_dimensions["A"].width = 6
            ws.column_dimensions["B"].width = 90
            for col in range(3, len(header) + 1):
                ws.column_dimensions[ws.cell(1, col).column_letter].width = 24
            for row in ws.iter_rows(min_row=2):
                for c in row:
                    c.alignment = Alignment(wrap_text=True, vertical="top")
            ws.freeze_panes = "C2"
            wb.save(path)
            return path
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(table)
    return path


def main():
    ap = argparse.ArgumentParser(description="插槽+素材池批量抽取提示词")
    ap.add_argument("spec")
    ap.add_argument("-o", "--output", default="prompts.xlsx")
    ap.add_argument("--count", type=int, help="指定条数；不填则生成素材池能支持的最大数量")
    ap.add_argument("--min-diff", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"不限条数时的安全上限，默认 {DEFAULT_LIMIT}")
    ap.add_argument("--tries", type=int, default=3000,
                    help="每条最多尝试多少个候选组合，默认 3000")
    ap.add_argument("--max-seconds", type=float, default=90,
                    help="最长运行时间（秒），到时间保存已生成的部分，默认 90")
    args = ap.parse_args()

    with open(args.spec, encoding="utf-8") as f:
        raw = json.load(f)
    count = args.count if args.count is not None else raw.get("count")
    min_diff = args.min_diff if args.min_diff is not None else raw.get("min_diff", 10)
    spec = Spec(raw, count, min_diff)
    check_template(spec)
    check_conflicts(spec)

    seed = args.seed if args.seed is not None else raw.get("seed")
    names, rows, usage, cap, min_seen, stop = generate(
        spec, random.Random(seed), args.limit, args.tries, args.max_seconds)

    if spec.count is not None and len(rows) < spec.count:
        why = (f"运行超过 {args.max_seconds:g} 秒" if stop == "timeout"
               else f"任意两条至少 {spec.min_diff} 处不同的要求下素材池不够")
        fail(f"{why}，只生成了 {len(rows)} 条，凑不够 {spec.count} 条。"
             f"请扩充素材池、增加维度、调低 min_diff，或加大 --max-seconds。没有输出文件")

    path = write_output(args.output, spec, names, rows)
    print(f"已生成 {len(rows)} 条 → {path}")
    if spec.count is None:
        if stop == "target":
            print(f"已到安全上限 {args.limit} 条，素材池还能支持更多。需要更多时加 --limit")
        elif stop == "timeout":
            print(f"运行到 {args.max_seconds:g} 秒时间上限后停止。越接近素材池容量越慢，"
                  f"说明已接近上限；需要更多时扩充素材池，或加大 --max-seconds")
        else:
            print(f"素材池在当前要求下最多支持约 {len(rows)} 条"
                  f"（连续 {args.tries} 个候选都不满足要求后停止）")
    print(f"差异维度共 {len(names)} 个，要求任意两条至少 {spec.min_diff} 处不同，"
          f"实际最小 {min_seen if min_seen is not None else '-'} 处")
    print("各维度使用次数（最少~最多）：")
    for n, c in spec.slots.items():
        cnt = [usage[n][p] for p in c["pool"]]
        print(f"  {n}: {min(cnt)}~{max(cnt)}（{len(c['pool'])} 项）")
    for g in spec.groups:
        for r, c in g["roles"].items():
            cnt = [usage[r][p] for p in c["pool"]]
            print(f"  {r}: {min(cnt)}~{max(cnt)}（{len(c['pool'])} 项）")
        for s, c in g["position_slots"].items():
            total = Counter()
            for p in g["positions"]:
                total.update(usage[f"{p}.{s}"])
            cnt = [total[v] for v in c["pool"]]
            print(f"  {s}（所有位置合计）: {min(cnt)}~{max(cnt)}（{len(c['pool'])} 项）")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
按 template/carousel.json 给每套轮播的每张画面写一条选项已经定好的提示词。

模型在一条提示词里看到"例如 A、B、C"时几乎总挑同一个，所以这里由脚本替它选：
每个可变点每条只写一个具体取值，取值在各套之间均衡轮换，同一张画面任意两套之间
尽量多处不同。质感和配色每套抽一次，三张共用，一套轮播看起来是同一个人拍的。

输出到一个新文件夹（不覆盖已有内容）：
    提示词/第01套-1-四宫格-提示词.txt ……   每条提示词，UTF-8，内容就是要提交的全文
    参考图/                                出图要带的产品图（--screenshot 时还有整张截图）
    jobs-试跑.json                         第 1 套，给 pinshuai-batch-image 的 render.py
    jobs-正式.json                         第 2 套起
    抽取明细.csv                           每条每个可变点取了什么、是不是原图做法
    batch.json                             深度、种子、各画面变了几处、实际最小差异

用法：
    python make_batch.py --sets 10                    # 默认深度 10
    python make_batch.py --sets 20 --depth 7 --seed 42
    python make_batch.py --sets 5 --slides 2 3 -o D:/出图任务/圣诞01
    python make_batch.py --check                      # 只检查模板，1～10 档逐档验证
"""
import argparse
import csv
import hashlib
import itertools
import json
import random
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sample_prompts as sp  # noqa: E402  抽取引擎：均衡取值 + 任意两条至少 N 处不同

SKILL_DIR = HERE.parent
DEFAULT_TEMPLATE = SKILL_DIR / "template" / "carousel.json"
SIZE = re.compile(r"^\d+x\d+$")


class TemplateError(Exception):
    pass


def text(t):
    return "\n".join(t) if isinstance(t, list) else t


def options(cfg, depth):
    """按深度返回这个可变点的取值：低于 band 只用原图；band～8 原图是选项之一；9～10 不放原图。"""
    pool = list(cfg["pool"])
    orig = cfg.get("original")
    if orig is None:
        return pool
    if depth < cfg.get("band", 1):
        return [orig]
    return pool if depth >= 9 else pool + [orig]


def sentinel(name):
    return f"\x00{name}\x00"


def build(tpl, slide, depth):
    """把一张画面的模板按深度转成 sample_prompts 的规格。"""
    body = text(slide["template"])
    for name, value in tpl.get("fixed", {}).items():
        body = body.replace("{{" + name + "}}", value)
    for name in tpl.get("shared", {}):
        body = body.replace("{{" + name + "}}", sentinel(name))
    fixed, originals = {}, {}
    raw = {"template": body, "slots": {}, "groups": []}

    for name, cfg in slide.get("slots", {}).items():
        originals[name] = cfg.get("original")
        opts = options(cfg, depth)
        if len(opts) == 1:
            fixed[name] = opts[0]
        else:
            raw["slots"][name] = {"pool": opts}

    for g in slide.get("groups", []):
        positions, open_ = g["positions"], depth >= g.get("band", 1)
        role_orig = {r: c["original"] for r, c in g["roles"].items()}
        originals.update(role_orig)
        pslots = {}
        for sname, cfg in g.get("position_slots", {}).items():
            per_pos = cfg["original"]
            for p in positions:
                originals[f"{p}.{sname}"] = per_pos[p]
            if depth < cfg.get("band", 1):
                fixed.update({f"{p}.{sname}": per_pos[p] for p in positions})
            else:
                pool = list(cfg["pool"])
                if depth < 9:
                    pool += [v for v in per_pos.values() if v not in pool]
                pslots[sname] = {"pool": pool, "distinct": cfg.get("distinct", True)}
        if not open_ and not pslots:
            fixed.update({p: role_orig[r] for p, r in zip(positions, g["roles"])})
            continue
        roles = {r: {"pool": options({**c, "band": g.get("band", 1)}, depth) if open_ else [c["original"]]}
                 for r, c in g["roles"].items()}
        raw["groups"].append({"positions": positions, "roles": roles,
                              "shuffle": open_ and g.get("shuffle", True), "position_slots": pslots,
                              "avoid_identity": depth >= 9})  # 9～10 档原图的宫格排法不进选项

    for name, value in fixed.items():
        raw["template"] = raw["template"].replace("{{" + name + "}}", value)
    raw["conflicts"] = prune_conflicts(slide.get("conflicts", []), fixed, raw)
    return raw, fixed, originals


def prune_conflicts(rules, fixed, raw):
    """固定下来的维度已经不在规格里：按固定值判断规则还会不会成立，再删掉这部分条件；
    还在变的维度只保留当前选项里有的取值。"""
    valid = {n: set(c["pool"]) for n, c in raw["slots"].items()}
    for g in raw["groups"]:
        content = set().union(*(set(r["pool"]) for r in g["roles"].values()))
        for p in g["positions"]:
            valid[p] = content
            for s, c in g["position_slots"].items():
                valid[f"{p}.{s}"] = set(c["pool"])
    out = []
    for rule in rules:
        kept, alive = {}, True
        for dim, vals in rule.items():
            vals = [vals] if isinstance(vals, str) else list(vals)
            if dim in fixed:
                alive = fixed[dim] in vals
            elif dim in valid:
                vals = [v for v in vals if v in valid[dim]]
                alive = bool(vals)
                kept[dim] = vals
            else:
                raise TemplateError(f"conflicts 引用了不存在的维度「{dim}」")
            if not alive:
                break
        if alive and kept:
            out.append(kept)
        elif alive:
            raise TemplateError(f"conflicts 规则在这个深度下必然成立，模板自相矛盾：{rule}")
    return out


def varied(spec):
    """一条提示词里真正会变的维度数（只有一个取值的维度不算）。"""
    n = sum(len(c["pool"]) > 1 for c in spec.slots.values())
    for g in spec.groups:
        n += sum(len(c["pool"]) > 1 for c in g["roles"].values())
        n += sum(len(c["pool"]) > 1 for c in g["position_slots"].values()) * len(g["roles"])
        n += 1 if g["shuffle"] else 0
    return n


def fixed_names(spec, fixed):
    """这个深度下固定不变的可变点：模板里直接替换掉的，加上规格里只剩一个取值的。"""
    names = list(fixed)
    names += [n for n, c in spec.slots.items() if len(c["pool"]) == 1]
    for g in spec.groups:
        names += [r for r, c in g["roles"].items() if len(c["pool"]) == 1]
    return names


def make_spec(raw, sets, min_diff):
    try:
        spec = sp.Spec(raw, sets, min_diff)
        sp.check_template(spec)
        sp.check_conflicts(spec)
    except SystemExit as e:  # sample_prompts 用 sys.exit 报错
        raise TemplateError(str(e)) from None
    return spec


def sample(raw, sets, target, seed, label, seconds):
    """从 target 处差异开始往下试，直到能凑够 sets 条。返回 (spec, rows, 用的差异要求)。"""
    probe = make_spec(raw, sets, 0)
    top = min(target, varied(probe))
    for md in range(top, -1, -1):
        spec = make_spec(raw, sets, md)
        rng = random.Random(f"{seed}|{label}|{md}")
        try:
            _, rows, *_ = sp.generate(spec, rng, sets, 3000, seconds)
        except SystemExit as e:
            raise TemplateError(str(e)) from None
        if len(rows) >= sets:
            return spec, rows, md
    raise TemplateError(f"{label}：选项之间的冲突太多，连 {sets} 条都凑不出来")


def deal_shared(tpl, sets, depth, rng):
    """质感、配色每套一个取值，均衡轮换。"""
    out = {}
    for name, cfg in tpl.get("shared", {}).items():
        opts = options(cfg, depth)
        seq = []
        while len(seq) < sets:
            batch = opts[:]
            rng.shuffle(batch)
            seq += batch
        out[name] = seq[:sets]
    return out


def render(spec, row, shared_values):
    s = sp.render(spec, row["values"])
    for name, value in shared_values.items():
        s = s.replace(sentinel(name), value)
    if "\x00" in s or sp.PH.search(s):
        raise TemplateError("渲染后还有没替换掉的占位符")
    return s


def min_pairwise(vectors):
    if len(vectors) < 2:
        return None
    return min(sum(a != b for a, b in zip(u, v)) for u, v in itertools.combinations(vectors, 2))


def validate(tpl, tpl_dir):
    """模板本身的检查，不看深度。"""
    errors, ids = [], set()
    shared, fixed_names = set(tpl.get("shared", {})), set(tpl.get("fixed", {}))

    def check_slot(where, name, cfg):
        pool = cfg.get("pool", [])
        if not pool or len(set(pool)) != len(pool):
            errors.append(f"{where}「{name}」的 pool 为空或有重复")
        if cfg.get("band", 1) > 1 and cfg.get("original") is None:
            errors.append(f"{where}「{name}」band>1 但没有 original，低档位没有原图做法可固定")
        if cfg.get("original") in pool:
            errors.append(f"{where}「{name}」的 original 不要再写进 pool，脚本会按深度自己加")

    for name, cfg in tpl.get("shared", {}).items():
        check_slot("shared", name, cfg)
    for slide in tpl["slides"]:
        sid = slide.get("id", "?")
        if sid in ids:
            errors.append(f"画面 id 重复：{sid}")
        ids.add(sid)
        if not SIZE.match(slide.get("size", "")):
            errors.append(f"{sid}：size 要写成 1024x1536 这样")
        for r in slide.get("refs", []) + ([slide["screenshot"]] if slide.get("screenshot") else []):
            if not (tpl_dir / r).is_file():
                errors.append(f"{sid}：找不到参考图 {r}")
        expected = set(slide.get("slots", {}))
        for name, cfg in slide.get("slots", {}).items():
            check_slot(sid, name, cfg)
        for g in slide.get("groups", []):
            positions = g["positions"]
            if len(g["roles"]) != len(positions):
                errors.append(f"{sid}：roles 数量要等于 positions 数量")
            for r, c in g["roles"].items():
                check_slot(sid, r, {**c, "band": g.get("band", 1)})
            for s, c in g.get("position_slots", {}).items():
                if set(c.get("original", {})) != set(positions):
                    errors.append(f"{sid}：位置插槽「{s}」的 original 要给每个位置各写一个")
                if c.get("distinct", True) and len(c["pool"]) < len(positions):
                    errors.append(f"{sid}：位置插槽「{s}」选项不够分给 {len(positions)} 个位置")
                expected |= {f"{p}.{s}" for p in positions}
            expected |= set(positions)
        clash = (expected | {r for g in slide.get("groups", []) for r in g["roles"]}) & (shared | fixed_names)
        if clash:
            errors.append(f"{sid}：这些名字和 shared/fixed 重名：{sorted(clash)}")
        used = set(sp.PH.findall(text(slide["template"])))
        if used - expected - shared - fixed_names:
            errors.append(f"{sid}：模板里有没定义的占位符 {sorted(used - expected - shared - fixed_names)}")
        if expected - used:
            errors.append(f"{sid}：这些可变点定义了但模板里没用到 {sorted(expected - used)}")
    return errors


def pick_slides(tpl, wanted):
    if not wanted:
        return tpl["slides"]
    out = []
    for w in wanted:
        hit = [s for s in tpl["slides"] if s["id"] == w or s["id"].startswith(w + "-")]
        if not hit:
            sys.exit(f"[错误] 模板里没有画面「{w}」，可选：{', '.join(s['id'] for s in tpl['slides'])}")
        out += [s for s in hit if s not in out]
    return out


def default_min_diff(n):
    return max(1, (n + 1) // 2)


def run_check(tpl, tpl_dir):
    errors = validate(tpl, tpl_dir)
    if errors:
        sys.exit("[错误] 模板有问题：\n  " + "\n  ".join(errors))
    print("深度  " + "  ".join(f"{s['id']}(变几处)" for s in tpl["slides"]))
    for depth in range(1, 11):
        cells = []
        for slide in tpl["slides"]:
            try:
                raw, _, _ = build(tpl, slide, depth)
                cells.append(str(varied(make_spec(raw, None, 0))))
            except TemplateError as e:
                sys.exit(f"[错误] 深度 {depth}，{slide['id']}：{e}")
        print(f"{depth:>4}  " + "  ".join(f"{c:>{len(s['id']) + 6}}" for c, s in zip(cells, tpl["slides"])))
    shared = sum(len(options(c, 10)) > 1 for c in tpl.get("shared", {}).values())
    print(f"模板检查通过。另有 {len(tpl.get('shared', {}))} 个每套共用的可变点（质感、配色），10 档时 {shared} 个会变。")


def csv_rows(set_no, slide_id, prompt_name, spec, row, fixed, originals, groups_cfg):
    out = []

    def add(dim, value, orig, how):
        out.append([set_no, slide_id, prompt_name, dim, value, how,
                    "" if orig is None else ("是" if value == orig else "否")])

    for n in spec.slots:
        add(n, row["values"][n], originals.get(n), "抽取")
    for gi, g in enumerate(spec.groups):
        cfg = next(c for c in groups_cfg if c["positions"] == g["positions"])
        home = dict(zip(cfg["roles"], g["positions"]))  # 每类内容在原图里的格子
        order = [p.split("=")[1] for p in row["dims"][f"排列{gi + 1}"].split("；")] if g["shuffle"] else list(g["roles"])
        if g["shuffle"]:
            natural = "；".join(f"{p}={r}" for r, p in home.items())
            add("宫格排列", row["dims"][f"排列{gi + 1}"], natural, "抽取")
        for p, r in zip(g["positions"], order):
            how = "抽取" if len(g["roles"][r]["pool"]) > 1 else "固定"
            add(f"{p}（{r}）", row["values"][p], originals.get(r), how)
            for s in g["position_slots"]:  # 原图做法按这类内容在原图里的拍法比，不按格子比
                add(f"{p}.{s}", row["values"][f"{p}.{s}"], originals.get(f"{home[r]}.{s}"), "抽取")
    for n, v in fixed.items():
        add(n, v, originals.get(n, v), "固定")
    return out


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="按三张轮播模板批量写提示词")
    ap.add_argument("--sets", type=int, help="出几套轮播（每套每张画面一条提示词）")
    ap.add_argument("--depth", type=int, default=10, help="重构深度 1～10，默认 10")
    ap.add_argument("--slides", nargs="+", help="只要某几张画面，写 id 或编号，比如 2 3")
    ap.add_argument("--seed", type=int, help="随机种子，同模板同种子结果可复现；不写就随机")
    ap.add_argument("--min-diff", type=int, help="同一张画面任意两套至少几处不同；默认是会变的可变点数的一半")
    ap.add_argument("--screenshot", action="store_true", help="另带整张截图做构图参考，只允许 1～6 档")
    ap.add_argument("--model", help="写进 jobs 文件的模型名，不写就用 render.py 的默认")
    ap.add_argument("--template", default=str(DEFAULT_TEMPLATE))
    ap.add_argument("-o", "--out", help="输出文件夹，默认 <当前目录>/圣诞珍珠画01-<时间>-深度N-M套")
    ap.add_argument("--max-seconds", type=float, default=30, help="每张画面每个差异要求最多试多久")
    ap.add_argument("--check", action="store_true", help="只检查模板，不写文件")
    args = ap.parse_args()

    tpl_path = Path(args.template).resolve()
    tpl_bytes = tpl_path.read_bytes()
    tpl = json.loads(tpl_bytes.decode("utf-8-sig"))
    tpl_dir = tpl_path.parent
    if args.check:
        return run_check(tpl, tpl_dir)
    errors = validate(tpl, tpl_dir)
    if errors:
        sys.exit("[错误] 模板有问题，先跑 --check：\n  " + "\n  ".join(errors))
    if not args.sets or args.sets < 1:
        sys.exit("[错误] 用 --sets 写要出几套")
    if not 1 <= args.depth <= 10:
        sys.exit("[错误] --depth 要是 1～10 的整数")
    if args.screenshot and args.depth > 6:
        sys.exit("[错误] 7～10 档不带整张截图：实测带截图时 4 张里 3 张沿用了原图布局")

    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(10 ** 6)
    slides = pick_slides(tpl, args.slides)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out).resolve() if args.out else \
        Path.cwd() / f"{tpl.get('name', 'batch')}-{stamp}-深度{args.depth}-{args.sets}套"
    if out.exists() and any(out.iterdir()):
        sys.exit(f"[错误] {out} 已经有内容，换一个 -o，不覆盖")

    shared_rng = random.Random(f"{seed}|shared")
    shared = deal_shared(tpl, args.sets, args.depth, shared_rng)
    width = max(2, len(str(args.sets)))
    (out / "提示词").mkdir(parents=True, exist_ok=True)
    (out / "参考图").mkdir(exist_ok=True)

    results, table, trial, formal = [], [], [], []
    for slide in slides:
        try:
            raw, fixed, originals = build(tpl, slide, args.depth)
            n_var = varied(make_spec(raw, None, 0))
            target = args.min_diff if args.min_diff is not None else default_min_diff(n_var)
            spec, rows, used_md = sample(raw, args.sets, target, seed, slide["id"], args.max_seconds)
        except TemplateError as e:
            sys.exit(f"[错误] {slide['id']}：{e}")
        refs = list(slide.get("refs", [])) + ([slide["screenshot"]] if args.screenshot and slide.get("screenshot") else [])
        ref_paths = []
        for r in refs:
            dst = out / "参考图" / Path(r).name
            if not dst.exists():
                shutil.copy2(tpl_dir / r, dst)
            ref_paths.append(f"参考图/{dst.name}")
        dims = spec.dim_names()
        vectors = []
        for i, row in enumerate(rows[:args.sets]):
            set_label = f"第{i + 1:0{width}d}套"
            sv = {n: shared[n][i] for n in shared}
            prompt = render(spec, row, sv)
            name = f"{set_label}-{slide['id']}"
            fname = f"{name}-提示词.txt"
            (out / "提示词" / fname).write_bytes(prompt.encode("utf-8"))
            job = {"name": name, "prompt": f"提示词/{fname}", "refs": ref_paths, "count": 1, "size": slide["size"]}
            (trial if i == 0 else formal).append(job)
            table += csv_rows(set_label, slide["id"], fname, spec, row, fixed, originals, slide.get("groups", []))
            for n in shared:
                orig = tpl["shared"][n].get("original")
                table.append([set_label, slide["id"], fname, n, sv[n], "每套共用",
                              "" if orig is None else ("是" if sv[n] == orig else "否")])
            vectors.append([row["dims"][d] for d in dims] + [sv[n] for n in shared])
        n_shared = sum(len(set(shared[n])) > 1 for n in shared)
        results.append({"id": slide["id"], "varied": n_var, "fixed": fixed_names(spec, fixed), "min_diff_target": target,
                        "min_diff_used": used_md, "min_diff_actual": min_pairwise(vectors),
                        "varied_with_shared": n_var + n_shared, "refs": ref_paths})

    def write_jobs(fname, jobs):
        spec = {"prompt_skill": tpl.get("name", "圣诞珍珠画01")}
        if args.model:
            spec["model"] = args.model
        spec["jobs"] = jobs
        (out / fname).write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    trial.sort(key=lambda j: j["name"])
    formal.sort(key=lambda j: j["name"])
    write_jobs("jobs-试跑.json", trial)
    if formal:
        write_jobs("jobs-正式.json", formal)
    with open(out / "抽取明细.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["套", "画面", "提示词文件", "可变点", "取值", "来源", "是原图做法"])
        w.writerows(table)
    meta = {"template": str(tpl_path), "template_sha256": hashlib.sha256(tpl_bytes).hexdigest(),
            "created": datetime.now().isoformat(timespec="seconds"), "depth": args.depth, "sets": args.sets,
            "seed": seed, "screenshot": args.screenshot, "slides": results,
            "jobs": {"trial": "jobs-试跑.json", "formal": "jobs-正式.json" if formal else None}}
    (out / "batch.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"深度 {args.depth}，{args.sets} 套，种子 {seed} → {out}")
    for r in results:
        note = "" if r["min_diff_used"] == r["min_diff_target"] else \
            f"（要求 {r['min_diff_target']} 处凑不够，降到 {r['min_diff_used']} 处）"
        actual = "-" if r["min_diff_actual"] is None else r["min_diff_actual"]
        print(f"  {r['id']}：会变 {r['varied']} 处 + 每套共用 {len(shared)} 处，固定 {len(r['fixed'])} 处；"
              f"任意两套至少 {actual} 处不同{note}；带图 {len(r['refs'])} 张")
    print(f"  提示词 {len(trial) + len(formal)} 条；试跑 jobs-试跑.json {len(trial)} 张"
          + (f"，正式 jobs-正式.json {len(formal)} 张" if formal else ""))
    if args.screenshot:
        print("[提醒] 带了整张截图：L008 记录过截图里的轮播圆点会被画进图里，出图后要检查")


if __name__ == "__main__":
    main()

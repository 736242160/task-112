#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_replace.py — 批量替换传播工具（纯 Python 标准库，单文件）

== 文档格式（行流） ==
  @define <name> = <value>     定义一个替换目标（独占一行，允许前导空白）
  其它行内可写 @ref <name>     引用目标；输出时展开为该目标的当前值

== 操作流格式（每行一条） ==
  <name> = <new_value>         把目标 name 的内容替换为 new_value
  空行与 # 开头的行忽略；值内允许出现 '='（按第一个 '=' 切分）

== 冲突规则（自定） ==
  同一目标出现多条替换操作且值不同：先到先得（first-wins），后续冲突
  操作被拒绝并报告；同值重复操作直接去重。理由：结果只依赖操作顺序，
  确定、可解释、可复现；避免“后写覆盖”让追加在末尾的机器生成操作
  悄悄覆盖掉排在前面的（通常是人工的）编辑。

== 回滚范围（自定） ==
  以“单条操作”为原子单位：一条操作会更新目标定义并把新值传播到所有
  引用位置，若其中任何一步失败（如传播后某行超长），撤销该操作已应用
  的全部修改（undo log 逐条回滚），其它互不影响的操作照常生效。
  理由：批量操作通常彼此独立，整批回滚会丢失合法修改；而半应用的
  操作必须回滚，否则文档会停留在定义与引用不一致的中间态。

== 格式校验 ==
  新值：非空、可打印、不含 '@'（防止注入伪指令）、长度 <= 200。
  传播后任一行长度 > 1000 视为应用失败。校验失败报告目标定义行与
  全部引用行的行号（1 起始）。

== 可追溯 ==
  每一处定义/引用更新都记录 trace：操作序号、目标、行号、位置类型、
  旧内容、新内容、状态（applied / rolled-back）。

用法：
  python3 batch_replace.py DOC OPS [--report FILE]   # 文档输出到 stdout
  python3 batch_replace.py --selftest                # 运行内置自测样例
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field

DEFINE_RE = re.compile(r"^\s*@define\s+([A-Za-z_][\w.-]*)\s*=\s*(.*?)\s*$")
REF_RE = re.compile(r"@ref\s+([A-Za-z_][\w.-]*)")
OP_RE = re.compile(r"^([A-Za-z_][\w.-]*)\s*=\s*(.*?)\s*$")

MAX_VALUE_LEN = 200
MAX_LINE_LEN = 1000


@dataclass
class Op:
    seq: int
    target: str
    value: str
    line_no: int


@dataclass
class Error:
    kind: str
    message: str
    target: str = ""
    op_seq: int = 0
    lines: list = field(default_factory=list)


@dataclass
class TraceEntry:
    op_seq: int
    target: str
    line_no: int
    site: str  # 'define' | 'ref'
    old: str
    new: str
    status: str = "applied"


@dataclass
class Document:
    lines: list
    defines: dict        # name -> [(line_idx, value)]
    refs: dict           # name -> [(line_idx, col)]
    define_owner: dict   # line_idx -> name


def validate_value(value: str):
    if not value:
        return "值为空"
    if len(value) > MAX_VALUE_LEN:
        return f"值长度 {len(value)} 超过上限 {MAX_VALUE_LEN}"
    if "@" in value:
        return "值含 '@'，可能注入伪指令"
    if any(ord(c) < 32 for c in value):
        return "值含控制字符"
    return None


def parse_document(text: str, errors: list) -> Document:
    lines = text.splitlines()
    defines, refs, define_owner = {}, {}, {}
    for idx, line in enumerate(lines):
        m = DEFINE_RE.match(line)
        if m:
            name, value = m.group(1), m.group(2)
            define_owner[idx] = name
            if name in defines:
                errors.append(Error(
                    "duplicate-define",
                    f"目标 {name!r} 在第 {idx + 1} 行重复定义，以首次定义为准",
                    target=name, lines=[idx + 1]))
            defines.setdefault(name, []).append((idx, value))
            continue
        if "@define" in line:
            errors.append(Error(
                "malformed-define",
                f"第 {idx + 1} 行 @define 语法非法，已按普通行处理",
                lines=[idx + 1]))
        for rm in REF_RE.finditer(line):
            refs.setdefault(rm.group(1), []).append((idx, rm.start()))
    return Document(lines, defines, refs, define_owner)


def parse_ops(text: str, errors: list) -> list:
    ops = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = OP_RE.match(line)
        if not m:
            errors.append(Error(
                "malformed-op",
                f"操作流第 {line_no} 行语法非法（应为 '<name> = <value>'），已跳过：{raw!r}",
                lines=[line_no]))
            continue
        ops.append(Op(len(ops) + 1, m.group(1), m.group(2), line_no))
    return ops


def render_line_at(doc: Document, idx: int, values: dict, unresolved=None) -> str:
    owner = doc.define_owner.get(idx)
    if owner is not None:
        line = doc.lines[idx]
        indent = line[:len(line) - len(line.lstrip())]
        return f"{indent}@define {owner} = {values[owner]}"

    def repl(m):
        name = m.group(1)
        if name in values:
            return values[name]
        if unresolved is not None:
            unresolved.append(name)
        return m.group(0)

    return REF_RE.sub(repl, doc.lines[idx])


def apply_op(doc: Document, values: dict, op: Op, trace: list, errors: list) -> None:
    name = op.target
    if name not in values:
        errors.append(Error(
            "target-not-found",
            f"第 {op.seq} 条操作：替换目标 {name!r} 在文档中不存在，已跳过",
            target=name, op_seq=op.seq, lines=[op.line_no]))
        return

    positions = sorted({i + 1 for i, _ in doc.defines[name]}
                       | {i + 1 for i, _ in doc.refs.get(name, [])})
    msg = validate_value(op.value)
    if msg:
        errors.append(Error(
            "invalid-value",
            f"第 {op.seq} 条操作：目标 {name!r} 替换后内容格式校验失败：{msg}",
            target=name, op_seq=op.seq, lines=positions))
        return

    old_value = values[name]
    affected = sorted({i for i, _ in doc.defines[name]}
                      | {i for i, _ in doc.refs.get(name, [])})
    old_rendered = {i: render_line_at(doc, i, values) for i in affected}

    undo = [(name, old_value)]
    values[name] = op.value

    op_trace = []
    failed_line = None
    for i in affected:
        rendered = render_line_at(doc, i, values)
        if len(rendered) > MAX_LINE_LEN:
            failed_line = i
            break
        site = "define" if doc.define_owner.get(i) == name else "ref"
        op_trace.append(TraceEntry(op.seq, name, i + 1, site,
                                   old_rendered[i], rendered))

    if failed_line is not None:
        for key, old in reversed(undo):  # 回滚本操作已应用的全部修改
            values[key] = old
        for entry in op_trace:
            entry.status = "rolled-back"
        trace.extend(op_trace)
        errors.append(Error(
            "apply-failed",
            f"第 {op.seq} 条操作：目标 {name!r} 传播后第 {failed_line + 1} 行长度"
            f"超过 {MAX_LINE_LEN}，本操作已整体回滚，其它操作不受影响",
            target=name, op_seq=op.seq, lines=[failed_line + 1]))
        return

    trace.extend(op_trace)


def run(doc_text: str, ops_text: str):
    errors, trace = [], []
    doc = parse_document(doc_text, errors)
    ops = parse_ops(ops_text, errors)
    values = {name: sites[0][1] for name, sites in doc.defines.items()}

    winners, order = {}, []
    for op in ops:
        first = winners.get(op.target)
        if first is not None:
            if first.value != op.value:
                errors.append(Error(
                    "conflict",
                    f"目标 {op.target!r} 的第 {op.seq} 条操作（值 {op.value!r}）与第 "
                    f"{first.seq} 条（值 {first.value!r}）冲突，按先到先得规则被拒绝",
                    target=op.target, op_seq=op.seq, lines=[op.line_no]))
            continue
        winners[op.target] = op
        order.append(op.target)

    for target in order:
        apply_op(doc, values, winners[target], trace, errors)

    out_lines = []
    for i in range(len(doc.lines)):
        unresolved = []
        out_lines.append(render_line_at(doc, i, values, unresolved))
        for name in unresolved:
            errors.append(Error(
                "unresolved-reference",
                f"第 {i + 1} 行引用了未定义的目标 {name!r}，已原样保留",
                target=name, lines=[i + 1]))

    return "\n".join(out_lines) + "\n", errors, trace


def format_report(errors: list, trace: list) -> str:
    out = ["== 错误报告 =="]
    if not errors:
        out.append("（无错误）")
    for e in errors:
        loc = f" [行: {', '.join(map(str, e.lines))}]" if e.lines else ""
        out.append(f"[{e.kind}] {e.message}{loc}")
    out.append("")
    out.append("== 传播追踪 ==")
    if not trace:
        out.append("（无更新）")
    for t in trace:
        out.append(f"op#{t.op_seq} {t.target} 行{t.line_no} ({t.site}) "
                   f"[{t.status}]: {t.old!r} -> {t.new!r}")
    applied = sum(1 for t in trace if t.status == "applied")
    out.append("")
    out.append(f"== 摘要 == 错误 {len(errors)} 条；传播更新 {applied} 处"
               f"（回滚 {len(trace) - applied} 处）")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- 自测样例

SAMPLE_DOC = """\
@define app_name = OldApp
@define version = 1.0
@define tag = v1
Welcome to @ref app_name!
Current version: @ref version
Again: @ref app_name rocks.
@ref missing_target should stay as-is.
"""

SAMPLE_OPS = """\
# 正常替换 + 传播
app_name = NewApp
# 冲突：与上面同目标不同值，应被拒绝（先到先得）
app_name = OtherApp
version = 2.0
# 目标不存在，应报告
ghost = 1
# 新值含 '@'，格式校验失败，应报告位置
tag = bad@value
"""

ROLLBACK_DOC = "@define long = x\n@define other = a\n" + " ".join(
    ["@ref long"] * 25) + "\nother=@ref other\n"
ROLLBACK_OPS = "long = " + "y" * 60 + "\nother = ok\n"


def selftest() -> int:
    failures = []

    def check(cond, label):
        print(("PASS" if cond else "FAIL"), "-", label)
        if not cond:
            failures.append(label)

    # 样例 1：传播 / 冲突 / 缺失目标 / 格式校验 / 未解析引用
    out, errors, trace = run(SAMPLE_DOC, SAMPLE_OPS)
    kinds = [e.kind for e in errors]

    check("Welcome to NewApp!" in out and "Again: NewApp rocks." in out,
          "引用随目标替换同步传播")
    check("Current version: 2.0" in out, "version 替换生效")
    check("OtherApp" not in out and kinds.count("conflict") == 1,
          "同目标冲突按先到先得拒绝并报告")
    check("target-not-found" in kinds, "替换不存在的目标被报告")
    iv = next((e for e in errors if e.kind == "invalid-value"), None)
    check(iv is not None and iv.lines == [3], "格式校验失败并报告定义行位置")
    check("unresolved-reference" in kinds and "@ref missing_target" in out,
          "文档内未定义引用原样保留并报告")
    check(sum(1 for t in trace if t.target == "app_name") == 3,
          "app_name 的 1 处定义 + 2 处引用均有追踪记录")
    check(all(t.status == "applied" for t in trace), "无失败操作时全部 applied")

    # 样例 2：部分失败 -> 单操作回滚，其它操作不受影响
    out2, errors2, trace2 = run(ROLLBACK_DOC, ROLLBACK_OPS)
    check(any(e.kind == "apply-failed" for e in errors2), "传播失败被报告")
    check("@define long = x" in out2, "失败操作的定义行已回滚")
    check("other=ok" in out2, "其它操作不受回滚影响（单操作原子）")
    rb = [t for t in trace2 if t.status == "rolled-back"]
    check(len(rb) == 1 and rb[0].target == "long" and rb[0].site == "define",
          "失败操作已应用的定义行留有 rolled-back 记录（引用行未提交）")

    # 样例 3：非法操作行 / 重复定义
    out3, errors3, _ = run("@define a = 1\n@define a = 2\n", "not an op\na = 9\n")
    check(any(e.kind == "malformed-op" for e in errors3), "非法操作行被报告并跳过")
    check(any(e.kind == "duplicate-define" for e in errors3), "重复定义被报告")
    check("@define a = 9" in out3, "合法操作在存在其它错误时仍生效")

    print()
    if failures:
        print(f"SELFTEST FAILED: {len(failures)} 项未通过")
        return 1
    print("ALL TESTS PASSED")
    print()
    print("---- 样例 1 输出文档 ----")
    print(out, end="")
    print("---- 样例 1 报告 ----")
    print(format_report(errors, trace), end="")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="批量替换传播工具")
    parser.add_argument("doc", nargs="?", help="文档文件路径")
    parser.add_argument("ops", nargs="?", help="替换操作文件路径")
    parser.add_argument("--report", help="错误报告输出路径（默认 stderr）")
    parser.add_argument("--selftest", action="store_true", help="运行内置自测样例")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.doc or not args.ops:
        parser.error("需要 DOC 与 OPS 两个文件参数，或使用 --selftest")

    with open(args.doc, encoding="utf-8") as f:
        doc_text = f.read()
    with open(args.ops, encoding="utf-8") as f:
        ops_text = f.read()

    out, errors, trace = run(doc_text, ops_text)
    sys.stdout.write(out)
    report = format_report(errors, trace)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report)
    else:
        sys.stderr.write(report)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

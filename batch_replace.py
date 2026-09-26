#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_replace.py — 批量替换与传播工具（纯 Python 标准库，单文件）

文档格式
--------
  定义行:  @def 名称: 当前值
  引用:    任意行内出现 @ref(名称)

操作流格式（每行一条：目标标识 = 替换值；空行与 # 开头行为注释）
----------------------------------------------------------------
  名称 = 新值

自定规则及理由
--------------
1. 冲突处理：同一目标出现多个不同取值 -> 先出现者生效，后续冲突操作
   报告为非致命错误并跳过。理由：结果确定、可重现，保留输入顺序的
   优先级语义，且不因可自动裁决的冲突阻断整批。
2. 未知目标 / 值格式非法 / 操作行无法解析 / 目标重复定义 -> 致命错误。
3. 回滚范围：整个批次原子化。任一致命错误 -> 已应用的修改全部回滚，
   输出原文档。理由：目标与引用相互关联，部分应用会留下目标已改而
   引用未同步（或反之）的不一致文档；冲突属非致命（存在确定性胜者），
   不触发回滚。
4. 值格式校验：新值必须匹配 ^[A-Za-z0-9._/-]+$（令牌式取值，防止
   注入换行或 @ref(...) 占位符语法破坏文档结构）。失败时报告目标
   定义行与全部引用所在行号。
5. 可追溯：每处定义更新与引用替换都记录 (行号, 目标, 旧值, 新值)。

用法
----
  python3 batch_replace.py 文档文件 操作文件 [--json]
  python3 batch_replace.py --selftest

退出码：0 成功（含非致命冲突）；1 用法错误；2 批次已回滚。
"""

import json
import re
import sys

DEF_RE = re.compile(r"^@def\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
REF_RE = re.compile(r"@ref\(([A-Za-z_][A-Za-z0-9_]*)\)")
OP_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
VALUE_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _issue(kind, fatal, message, target=None, op_line=None, positions=None):
    return {
        "kind": kind,
        "fatal": fatal,
        "message": message,
        "target": target,
        "op_line": op_line,
        "positions": positions or [],
    }


def apply_batch(doc_lines, op_lines):
    """应用整批操作，返回 (结果行列表, 报告字典)。失败时结果等于原文档。"""
    errors, warnings, trace = [], [], []

    defs = {}
    for idx, line in enumerate(doc_lines):
        m = DEF_RE.match(line)
        if not m:
            continue
        name = m.group(1)
        if name in defs:
            errors.append(_issue(
                "duplicate-definition", True,
                "目标 %r 重复定义：首次在第 %d 行，再次在第 %d 行"
                % (name, defs[name][0] + 1, idx + 1),
                target=name, positions=[defs[name][0] + 1, idx + 1]))
        else:
            defs[name] = (idx, m.group(2))

    ref_lines = {}
    for idx, line in enumerate(doc_lines):
        for m in REF_RE.finditer(line):
            ref_lines.setdefault(m.group(1), []).append(idx)

    parsed_ops = []
    for j, raw in enumerate(op_lines):
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        m = OP_RE.match(raw)
        if not m:
            errors.append(_issue("malformed-operation", True,
                                 "无法解析的操作行: %r" % raw, op_line=j + 1))
            continue
        parsed_ops.append((j, m.group(1), m.group(2)))

    chosen = {}
    for j, name, value in parsed_ops:
        if name in chosen:
            first_j, first_v = chosen[name]
            if first_v != value:
                errors.append(_issue(
                    "conflict", False,
                    "目标 %r 取值冲突：第 %d 行操作的值 %r 被忽略，"
                    "采用第 %d 行的值 %r（先到先生效）"
                    % (name, j + 1, value, first_j + 1, first_v),
                    target=name, op_line=j + 1))
        else:
            chosen[name] = (j, value)

    for name, (j, value) in chosen.items():
        if name not in defs:
            errors.append(_issue("unknown-target", True,
                                 "操作指向不存在的目标 %r" % name,
                                 target=name, op_line=j + 1))
            continue
        if not VALUE_RE.match(value):
            positions = sorted({defs[name][0] + 1}
                               | {i + 1 for i in ref_lines.get(name, [])})
            errors.append(_issue(
                "invalid-value-format", True,
                "目标 %r 的新值 %r 不符合格式 %s" % (name, value, VALUE_RE.pattern),
                target=name, op_line=j + 1, positions=positions))

    if any(e["fatal"] for e in errors):
        report = {"rolled_back": True, "rollback_scope": "entire-batch",
                  "errors": errors, "warnings": warnings, "trace": trace}
        return list(doc_lines), report

    new_lines = list(doc_lines)
    for name, (j, value) in chosen.items():
        def_idx, old_value = defs[name]
        new_lines[def_idx] = "@def %s: %s" % (name, value)
        trace.append({"line": def_idx + 1, "kind": "definition",
                      "target": name, "old": old_value, "new": value})

    chosen_values = {name: v for name, (j, v) in chosen.items()}

    def substitute(line, lineno):
        def repl(m):
            name = m.group(1)
            if name in chosen_values:
                new = chosen_values[name]
                trace.append({"line": lineno, "kind": "reference",
                              "target": name, "old": m.group(0), "new": new})
                return new
            if name not in defs:
                warnings.append({
                    "kind": "undefined-reference",
                    "message": "第 %d 行引用了未定义的目标 %r，保持原样" % (lineno, name),
                    "line": lineno, "target": name})
            return m.group(0)
        return REF_RE.sub(repl, line)

    for idx, line in enumerate(new_lines):
        if "@ref(" in line:
            new_lines[idx] = substitute(line, idx + 1)

    report = {"rolled_back": False, "rollback_scope": "entire-batch",
              "errors": errors, "warnings": warnings, "trace": trace}
    return new_lines, report


def print_report(report, stream, as_json=False):
    if as_json:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        return
    status = "已回滚（整批原子回滚）" if report["rolled_back"] else "已应用"
    stream.write("== 错误报告 ==\n状态: %s\n" % status)
    if not report["errors"]:
        stream.write("错误: 无\n")
    for e in report["errors"]:
        level = "致命" if e["fatal"] else "非致命"
        pos = (" 位置: 行 %s" % ",".join(map(str, e["positions"]))) if e["positions"] else ""
        op = (" 操作行: %d" % e["op_line"]) if e["op_line"] else ""
        stream.write("错误[%s|%s]%s%s %s\n" % (e["kind"], level, op, pos, e["message"]))
    for w in report["warnings"]:
        stream.write("警告[%s] %s\n" % (w["kind"], w["message"]))
    stream.write("== 传播追踪 ==\n")
    if not report["trace"]:
        stream.write("(无更新)\n")
    for t in report["trace"]:
        kind = "定义" if t["kind"] == "definition" else "引用"
        stream.write("行 %d [%s] %s: %r -> %r\n"
                     % (t["line"], kind, t["target"], t["old"], t["new"]))


def selftest():
    doc = [
        "@def host: 192.168.0.1",
        "@def port: 8080",
        "server address: @ref(host)",
        "connect to @ref(host):@ref(port) now",
    ]

    def expect(cond, msg):
        if not cond:
            raise AssertionError(msg)

    # 1. 正常传播
    result, rep = apply_batch(doc, ["host = 10.0.0.8", "port = 9090"])
    expect(not rep["rolled_back"] and not rep["errors"], "case1: 不应有错误")
    expect(result == ["@def host: 10.0.0.8", "@def port: 9090",
                      "server address: 10.0.0.8",
                      "connect to 10.0.0.8:9090 now"], "case1: 传播结果错误")
    expect(len([t for t in rep["trace"] if t["kind"] == "definition"]) == 2,
           "case1: 定义追踪条数")
    expect(len([t for t in rep["trace"] if t["kind"] == "reference"]) == 3,
           "case1: 引用追踪条数")
    print("PASS: 1 正常替换与引用传播（含追踪记录）")

    # 2. 同目标冲突：先到先生效，非致命
    result, rep = apply_batch(doc, ["host = 10.0.0.8", "host = 10.0.0.9"])
    expect(not rep["rolled_back"], "case2: 冲突不应回滚")
    expect(result[0] == "@def host: 10.0.0.8", "case2: 先到先生效")
    expect(len(rep["errors"]) == 1 and rep["errors"][0]["kind"] == "conflict"
           and not rep["errors"][0]["fatal"], "case2: 应报告非致命冲突")
    print("PASS: 2 同目标冲突 -> 先到先生效并报告")

    # 3. 未知目标：致命，整批回滚
    result, rep = apply_batch(doc, ["ghost = 1.1.1.1"])
    expect(rep["rolled_back"] and result == doc, "case3: 应回滚且文档不变")
    expect(rep["errors"][0]["kind"] == "unknown-target", "case3: 错误类型")
    print("PASS: 3 替换不存在的目标 -> 报告并回滚")

    # 4. 值格式非法：报告位置（定义行 + 所有引用行），回滚
    result, rep = apply_batch(doc, ["host = 10.0.0.8 abc"])
    expect(rep["rolled_back"] and result == doc, "case4: 应回滚")
    e = rep["errors"][0]
    expect(e["kind"] == "invalid-value-format" and e["positions"] == [1, 3, 4],
           "case4: 应报告定义行与引用行位置, 实际 %s" % e)
    print("PASS: 4 值格式校验失败 -> 报告位置并回滚")

    # 5. 混合批次：一条合法 + 一条致命 -> 整批回滚（合法修改也撤销）
    result, rep = apply_batch(doc, ["port = 9090", "ghost = x"])
    expect(rep["rolled_back"] and result == doc, "case5: 整批原子回滚")
    expect(rep["trace"] == [], "case5: 回滚后无追踪记录")
    print("PASS: 5 部分失败 -> 整批原子回滚")

    # 6. 引用未定义目标：警告，不致命
    doc2 = doc + ["note: @ref(nothing) here"]
    result, rep = apply_batch(doc2, ["host = 10.0.0.8"])
    expect(not rep["rolled_back"] and result[4] == "note: @ref(nothing) here",
           "case6: 未定义引用保持原样")
    expect(len(rep["warnings"]) == 1
           and rep["warnings"][0]["kind"] == "undefined-reference",
           "case6: 应有未定义引用警告")
    print("PASS: 6 引用未定义目标 -> 警告并保持原样")

    print("\nALL SELF-TESTS PASSED")

    print("\n== 演示（场景 1 的输出与报告）==")
    result, rep = apply_batch(doc, ["host = 10.0.0.8", "port = 9090"])
    print("--- 替换后文档 ---")
    print("\n".join(result))
    print("--- 报告 ---")
    print_report(rep, sys.stdout)
    return 0


def main(argv):
    if "--selftest" in argv:
        return selftest()
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 2:
        sys.stderr.write(__doc__)
        return 1
    with open(args[0], encoding="utf-8") as f:
        doc_lines = f.read().splitlines()
    with open(args[1], encoding="utf-8") as f:
        op_lines = f.read().splitlines()
    result, report = apply_batch(doc_lines, op_lines)
    sys.stdout.write("\n".join(result) + "\n")
    print_report(report, sys.stderr, as_json)
    return 2 if report["rolled_back"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

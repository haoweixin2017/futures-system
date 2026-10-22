#!/usr/bin/env python
"""前端 DOM 引用一致性检查。

专治这类事故: JS 里 getElementById('x') / querySelector('#x') 引用的 id
在 HTML 中根本不存在 —— 症状是「点击毫无反应」, 因为处理函数在
el.style / el.textContent 上抛 TypeError, 而页面不弹任何错。

用法:
    ./.venv/Scripts/python.exe scripts/check_dom_refs.py            # 只报缺失
    ./.venv/Scripts/python.exe scripts/check_dom_refs.py --strict   # 缺失即退出码 1

退出码: --strict 下有缺失返回 1, 否则 0。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [
    ROOT / "frontend" / "index.html",
    ROOT / "frontend" / "live.html",
    ROOT / "frontend" / "login.html",
]

# 这些 id 由 JS 在运行时动态创建 (菜单/浮层), 不在静态 HTML 里, 属正常
DYNAMIC_IDS = {
    "contract-menu",      # showContractMenu / showGroupMenu 动态 appendChild
    "group-picker",       # 历史遗留 (已移除), 保留以兼容旧检查
}


def collect_defined_ids(text: str) -> set:
    """HTML 中静态定义的 id (含字符串模板里生成的, 因为那也是页面真实元素)"""
    return set(re.findall(r"""\bid\s*=\s*["']([^"']+)["']""", text))


def collect_referenced_ids(text: str) -> set:
    """JS/HTML 中通过 getElementById / querySelector('#x') 引用的 id。

    只收「完整的字符串字面量」—— 拼接/模板的片段 (如 getElementById('price-' + sym)、
    `#tab-${name}`) 不是固定 id, 跳过, 否则会产生大量假阳性。
    """
    refs = set()
    # getElementById('x') / getElementById("x") —— 引号后必须紧跟右括号才算完整字面量
    refs |= set(re.findall(r"""getElementById\(\s*["']([^"'\s]+)["']\s*\)""", text))
    # querySelector('#x') / querySelectorAll('#x') —— 同样要求引号后紧跟右括号
    refs |= set(re.findall(r"""querySelector(?:All)?\(\s*["']#([A-Za-z_][\w-]*)["']\s*\)""", text))
    return refs


def collect_inline_handler_fns(text: str) -> set:
    """内联 on*="fnName(" 里引用的函数名, 用来发现「调用了不存在的函数」。

    只取 `fn(` 形式且前面是属性起始/分号/空格 的裸调用,
    排除 `obj.method(`、`x.stopPropagation(` 这类方法调用。
    """
    fns = set()
    for attr in re.findall(r"""\bon\w+\s*=\s*["']([^"']*)["']""", text):
        # 去掉字符串里的方法调用链: 匹配 前面不是 . 也不是标识符字符 的 fn(
        fns |= set(re.findall(r"""(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(""", attr))
    return fns


def collect_defined_fns(text: str) -> set:
    fns = set()
    fns |= set(re.findall(r"""\bfunction\s+([A-Za-z_$][\w$]*)\s*\(""", text))
    fns |= set(re.findall(r"""\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(|function)""", text))
    fns |= set(re.findall(r"""\b([A-Za-z_$][\w$]*)\s*:\s*(?:async\s*)?(?:\(|function)""", text))
    return fns


# 内联 handler 里常见的非自定义函数 (浏览器/语言内置), 不参与「未定义」判定
BUILTIN_FNS = {
    "event", "this", "alert", "confirm", "prompt", "parseInt", "parseFloat", "String",
    "Number", "Boolean", "Array", "Object", "JSON", "Math", "Date", "if", "for", "while",
    "switch", "catch", "return", "typeof", "new", "function", "setTimeout", "encodeURIComponent",
    "decodeURIComponent", "isNaN", "isFinite", "void", "delete", "await", "async", "in",
}


def main() -> int:
    strict = "--strict" in sys.argv
    total_missing = 0

    for path in TARGETS:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        defined = collect_defined_ids(text)
        refs = collect_referenced_ids(text)
        missing = sorted(refs - defined - DYNAMIC_IDS)

        defined_fns = collect_defined_fns(text)
        called = collect_inline_handler_fns(text)
        undef_fns = sorted(
            f for f in called - defined_fns - BUILTIN_FNS
            if not f.startswith("$") and f not in {"window", "document", "e"}
        )

        rel = path.relative_to(ROOT)
        if missing or undef_fns:
            print(f"\n{rel}")
            for i in missing:
                print(f"  [缺失 id]     #{i}  ← getElementById/querySelector 引用了不存在的元素")
            for f in undef_fns:
                print(f"  [未定义函数]  {f}()  ← 内联 on* 处理器里调用, 但未找到定义")
            total_missing += len(missing) + len(undef_fns)
        else:
            print(f"{rel}  OK ({len(refs)} 个 id 引用, {len(called)} 个内联函数调用全部可解析)")

    print()
    if total_missing:
        print(f"发现 {total_missing} 处不一致 —— 这类问题会导致「点击毫无反应」且不弹错。")
        return 1 if strict else 0
    print("DOM 引用一致, 未发现悬空引用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

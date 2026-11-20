"""生成仓库根目录的 plugins.json：VideoTurbo 的“插件仓库”按它列出本仓库的插件。

只做静态读取（解析源码，不执行插件代码，也不需要安装 VideoTurbo）：

- 插件 id、名称、版本、主页、图标、说明：优先读模块常量 ``PLUGIN_ID``、``PLUGIN_NAME``、``PLUGIN_VERSION``、
  ``PLUGIN_HOMEPAGE``、``PLUGIN_ICON``、``PLUGIN_DESCRIPTION``；文件里只有一个插件类时，没写的取该类的
  ``id``、``label``、``version``、``homepage``、``icon``、``description``；说明再没有时取模块文档字符串
  正文（第一段是标题时取第二段）的第一句。
- 类型：按插件类继承的基类（CloudProvider、MediaServer、MediaSourcePlugin、Notifier），一个文件可以有多种。
- ``PLUGIN_ICON`` 是 https 地址时下载图标并内嵌为 data URI，控制台显示仓库列表时不访问图片地址。

用法（在仓库根目录）::

    python tools/build_index.py           # 重新生成 plugins.json
    python tools/build_index.py --check   # 只检查 plugins.json 是否最新，不是最新时退出码为 1（用于 CI）
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "plugins.json"
REPOSITORY_NAME = "VideoTurbo 官方插件"
# 不是插件的目录。
SKIP_DIRS = {"tests", "tools", ".git", ".venv", "__pycache__"}
KINDS = {
    "CloudProvider": "cloud",
    "MediaServer": "media_server",
    "MediaSourcePlugin": "media_source",
    "Notifier": "notifier",
}
ICON_MAX_BYTES = 48 * 1024 - 64
_RASTER = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"), (b"GIF8", "gif"))


def _constants(tree: ast.Module) -> dict[str, object]:
    found: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                found[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return found


def _class_attrs(node: ast.ClassDef, constants: dict[str, object]) -> dict[str, object]:
    attrs: dict[str, object] = {}
    for item in node.body:
        if not (isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name)):
            continue
        if isinstance(item.value, ast.Constant):
            attrs[item.targets[0].id] = item.value.value
        elif isinstance(item.value, ast.Name) and item.value.id in constants:
            attrs[item.targets[0].id] = constants[item.value.id]
    return attrs


def _bases(node: ast.ClassDef) -> set[str]:
    return {base.id if isinstance(base, ast.Name) else base.attr for base in node.bases if isinstance(base, (ast.Name, ast.Attribute))}


def _icon(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    value = value.strip()
    if value.startswith("data:image/"):
        return value
    if value.startswith(("<svg", "<?xml")):
        return "data:image/svg+xml;base64," + base64.b64encode(value.encode()).decode()
    if not value.startswith("https://"):
        return ""
    with urllib.request.urlopen(value, timeout=30) as response:  # noqa: S310 - 只有 https 地址
        data = response.read(ICON_MAX_BYTES + 1)
    if len(data) > ICON_MAX_BYTES:
        raise ValueError(f"图标 {value} 超过 {ICON_MAX_BYTES // 1024} KB")
    kind = next((name for magic, name in _RASTER if data.startswith(magic)), "")
    if not kind and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = "webp"
    if not kind and b"<svg" in data[:1024]:
        kind = "svg+xml"
    if not kind:
        raise ValueError(f"图标 {value} 不是 PNG、JPEG、GIF、WebP 或 SVG 图片")
    return f"data:image/{kind};base64," + base64.b64encode(data).decode()


def _docstring_summary(tree: ast.Module) -> str:
    """模块文档字符串的说明：第一段是标题时取第二段，只取第一句。"""
    paragraphs = [part.replace("\n", "").strip() for part in (ast.get_docstring(tree) or "").split("\n\n")]
    paragraphs = [part for part in paragraphs if part]
    if not paragraphs:
        return ""
    text = paragraphs[1] if len(paragraphs) > 1 else paragraphs[0]
    return text.split("。")[0] + "。" if "。" in text else text


def describe(path: Path) -> dict[str, object] | None:
    """一个插件文件在 plugins.json 中的条目；不是插件时返回 None。"""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, str(path))
    constants = _constants(tree)
    if "PLUGIN_API" not in constants:
        return None
    members = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            kind = next((KINDS[name] for name in _bases(node) if name in KINDS), None)
            if kind:
                members.append((kind, _class_attrs(node, constants)))
    if not members:
        return None
    single = members[0][1] if len(members) == 1 else {}

    def declared(module_key: str, class_key: str) -> str:
        value = constants.get(module_key)
        if not (isinstance(value, str) and value.strip()):
            value = single.get(class_key)
        return value.strip() if isinstance(value, str) else ""

    relative = path.relative_to(ROOT).as_posix()
    plugin_id = declared("PLUGIN_ID", "id")
    label = declared("PLUGIN_NAME", "label")
    if not plugin_id or not label:
        raise ValueError(f"{relative}：有多个插件类时请用 PLUGIN_ID 和 PLUGIN_NAME 声明插件的 id 和名称")
    description = declared("PLUGIN_DESCRIPTION", "description") or _docstring_summary(tree)
    kinds = list(dict.fromkeys(kind for kind, _ in members))
    entry: dict[str, object] = {
        "id": plugin_id,
        "name": label,
        "version": declared("PLUGIN_VERSION", "version"),
        "description": description,
        "kinds": kinds,
        "path": relative,
        "sha256": hashlib.sha256(source.encode()).hexdigest(),
    }
    if homepage := declared("PLUGIN_HOMEPAGE", "homepage"):
        entry["homepage"] = homepage
    icon_value = constants.get("PLUGIN_ICON") or single.get("icon")
    if icon := _icon(icon_value):
        entry["icon"] = icon
    return entry


def build() -> str:
    plugins = []
    for path in sorted(ROOT.rglob("*.py")):
        if SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        if entry := describe(path):
            plugins.append(entry)
    ids = [entry["id"] for entry in plugins]
    if duplicated := sorted({plugin_id for plugin_id in ids if ids.count(plugin_id) > 1}):
        raise ValueError(f"插件 id 重复：{', '.join(map(str, duplicated))}")
    return json.dumps({"name": REPOSITORY_NAME, "plugins": plugins}, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="只检查 plugins.json 是否最新")
    args = parser.parse_args()
    try:
        content = build()
    except (ValueError, OSError, SyntaxError) as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 2
    if args.check:
        if not INDEX.exists() or INDEX.read_text(encoding="utf-8") != content:
            print("plugins.json 不是最新的，请运行 python tools/build_index.py", file=sys.stderr)
            return 1
        print("plugins.json 已是最新。")
        return 0
    INDEX.write_text(content, encoding="utf-8")
    print(f"已生成 {INDEX.relative_to(ROOT)}，共 {content.count('\"path\"')} 个插件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

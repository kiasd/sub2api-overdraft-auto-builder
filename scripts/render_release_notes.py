#!/usr/bin/env python3
"""Render deterministic release notes from a build metadata file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    inputs = metadata["inputs"]
    official = inputs["official"]
    fork = inputs["fork"]
    provenance = metadata["provenance"]
    build = metadata["build"]
    text = f"""# Sub2API 融合原生构建 {metadata['release_version']}

此产物由工作流自动锁定两个上游提交、应用二次元 UI overlay，并在测试通过后发布。

## 来源

- 官方项目：[Wei-Shaw/sub2api](https://github.com/{official['repository']})
- 官方版本：[`{official['tag']}`]({official['url']})，提交 [`{official['commit']}`](https://github.com/{official['repository']}/commit/{official['commit']})
- 透支项目：[DeanZFC/sub2api-overdraft](https://github.com/{fork['repository']}/tree/{fork['branch']})
- 透支版本：`{fork['version']}`，提交 [`{fork['commit']}`]({fork['url']})

## 构建结果

- 融合模式：`{provenance['integration_mode']}`
- UI 模式：`{provenance['overlay_mode']}`，来源版本 `{provenance['overlay_source_version']}`
- UI 清单：`{provenance['overlay_manifest_sha256']}`
- 二进制 SHA-256：`{build['binary_sha256']}`
- 测试：`{build['tests']}`

这是 Linux amd64 原生构建，不包含 Docker。下载后必须先核对 `SHA256SUMS`；部署端仍应执行程序与数据库备份、迁移兼容检查和健康检查，再由人工确认原子切换。

本仓库不是两个上游项目的官方发布渠道，许可证与归属见 `LICENSE` 和 `NOTICE`。
"""
    args.output.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

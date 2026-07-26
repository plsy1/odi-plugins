"""Junk file cleanup plugin.

Scans the configured download roots (or a caller-supplied path) for files
whose names match the qBittorrent keyword filter, and deletes the ones the
user selects. Deletion is doubly guarded: a path must resolve inside an
allowed root AND its filename must still match the filter, so neither
traversal tricks nor stale paths can remove anything else.
"""

import os
from datetime import datetime
from pathlib import Path

from fastapi import Body, HTTPException

SCAN_LIMIT = 2000


class Plugin:
    def setup(self, ctx):
        self.ctx = ctx

        @ctx.router.post("/scan")
        async def scan(path: str = Body("", embed=True)):
            try:
                return self._scan(path.strip() or None)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="路径不存在或不是目录",
                )

        @ctx.router.post("/delete")
        async def delete(
            paths: list[str] = Body(..., embed=True),
            path: str = Body("", embed=True),
        ):
            if len(paths) > SCAN_LIMIT:
                raise HTTPException(status_code=422, detail="选择的文件过多")
            return self._delete(paths, path.strip() or None)

    # -- helpers ---------------------------------------------------------
    def _keywords(self) -> list[str]:
        raw = self.ctx.get_config("QB_KEYWORD_FILTER", [])
        values = raw.split(",") if isinstance(raw, str) else (raw or [])
        return [
            str(value).strip().casefold()
            for value in values
            if str(value).strip()
        ]

    def _default_roots(self) -> list[Path]:
        roots: list[Path] = []
        for key in ("DOWNLOAD_PATH", "JAV_DOWNLOAD_PATH", "FC2_DOWNLOAD_PATH"):
            value = str(self.ctx.get_config(key, "") or "").strip()
            if not value:
                continue
            try:
                resolved = Path(value).expanduser().resolve()
            except OSError:
                continue
            if resolved.is_dir() and resolved not in roots:
                roots.append(resolved)
        # Drop roots nested inside another root so files are listed once.
        return [
            root
            for root in roots
            if not any(
                other != root and root.is_relative_to(other) for other in roots
            )
        ]

    def _custom_root(self, path_value: str) -> Path:
        try:
            resolved = Path(path_value).expanduser().resolve()
        except OSError as exc:
            raise ValueError("invalid path") from exc
        if not resolved.is_dir():
            raise ValueError("not a directory")
        return resolved

    def _scan(self, custom_path: str | None) -> dict:
        keywords = self._keywords()
        roots = (
            [self._custom_root(custom_path)]
            if custom_path
            else self._default_roots()
        )

        items: list[dict] = []
        truncated = False
        if keywords:
            for root in roots:
                if truncated:
                    break
                for dirpath, _dirnames, filenames in os.walk(root):
                    if truncated:
                        break
                    for filename in filenames:
                        if not any(
                            kw in filename.casefold() for kw in keywords
                        ):
                            continue
                        full_path = Path(dirpath) / filename
                        try:
                            stat = full_path.stat()
                        except OSError:
                            continue
                        items.append(
                            {
                                "path": str(full_path),
                                "name": filename,
                                "relative_path": str(
                                    full_path.relative_to(root)
                                ),
                                "size": stat.st_size,
                                "modified_at": datetime.fromtimestamp(
                                    stat.st_mtime
                                ).isoformat(),
                                "root": str(root),
                            }
                        )
                        if len(items) >= SCAN_LIMIT:
                            truncated = True
                            break

        items.sort(key=lambda item: item["size"], reverse=True)
        message = None
        if not keywords:
            message = "未配置关键词过滤，请先在「服务连接 → qBittorrent」中设置。"
        elif truncated:
            message = f"结果过多，仅显示前 {SCAN_LIMIT} 条。"
        elif roots:
            message = f"扫描目录：{', '.join(str(r) for r in roots)}"

        return {"items": items, "message": message}

    def _delete(self, paths: list[str], custom_path: str | None) -> dict:
        keywords = self._keywords()
        roots = self._default_roots()
        if custom_path:
            try:
                custom_root = self._custom_root(custom_path)
            except ValueError:
                custom_root = None
            if custom_root is not None and custom_root not in roots:
                roots.append(custom_root)

        deleted: list[str] = []
        failed: list[dict] = []
        for raw_path in paths:
            display = str(raw_path)
            try:
                resolved = Path(display).expanduser().resolve(strict=True)
            except OSError:
                failed.append({"path": display, "reason": "not found"})
                continue
            if not any(resolved.is_relative_to(root) for root in roots):
                failed.append(
                    {"path": display, "reason": "outside download roots"}
                )
                continue
            if not resolved.is_file():
                failed.append(
                    {"path": display, "reason": "not a regular file"}
                )
                continue
            if not keywords or not any(
                kw in resolved.name.casefold() for kw in keywords
            ):
                failed.append(
                    {"path": display, "reason": "name does not match filter"}
                )
                continue
            try:
                resolved.unlink()
                deleted.append(str(resolved))
            except OSError as exc:
                failed.append({"path": display, "reason": str(exc)})

        if failed:
            self.ctx.log_error(f"删除失败 {len(failed)} 项: {failed[:5]}")
        message = f"已删除 {len(deleted)} 个文件。"
        if failed:
            message += f" {len(failed)} 个失败（详见日志）。"
        return {"removed": deleted, "message": message}

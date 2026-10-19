"""Database persistence for hosts with an ephemeral disk (Render free tier).

The SQLite file is snapshotted (consistent, via the sqlite3 backup API), gzipped and sent to the
account's own "Saved Messages" as a document tagged with TAG. On a fresh start (empty/missing
database) the newest snapshot is downloaded and installed before anything else runs.
"""
import asyncio
import gzip
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone

from .config import settings
from .storage import Storage

TAG = "#watermelon_db_backup"
KEEP = 2
WAKE = asyncio.Event()
_lock = asyncio.Lock()
_state = {"client": None, "mtime": 0}


def request_backup():
    """Ask the backup loop to upload soon (used after rule changes so they survive a redeploy)."""
    WAKE.set()


def _is_fresh(path: str) -> bool:
    if not os.path.exists(path):
        return True
    try:
        con = sqlite3.connect(path, timeout=30)
        try:
            for table in ("messages", "profiles", "chat_rules", "person_rules"):
                if con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                    return False
        finally:
            con.close()
        return True
    except sqlite3.Error:
        return True


def _snapshot_gz(path: str, workdir: str) -> str:
    snap = os.path.join(workdir, "snapshot.db")
    src = sqlite3.connect(path, timeout=30)
    dst = sqlite3.connect(snap)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    name = "watermelon-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".db.gz"
    gz_path = os.path.join(workdir, name)
    with open(snap, "rb") as f_in, gzip.open(gz_path, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(snap)
    return gz_path


def _install(gz_path: str, path: str) -> bool:
    """Unpack, verify and atomically put the snapshot in place."""
    tmp = path + ".restore"
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with gzip.open(gz_path, "rb") as f_in, open(tmp, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    try:
        con = sqlite3.connect(tmp)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            ok = ok and con.execute("SELECT name FROM sqlite_master WHERE name='messages'").fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        ok = False
    if not ok:
        os.remove(tmp)
        return False
    os.replace(tmp, path)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    return True


async def _find_backups(client) -> list:
    found = []
    async for message in client.iter_messages("me", search=TAG, limit=30):
        name = (message.file.name if message.file else "") or ""
        if message.file and name.endswith(".db.gz"):
            found.append(message)
    return found  # newest first


async def restore_if_fresh(client, path: str) -> bool:
    if not await asyncio.to_thread(_is_fresh, path):
        return False
    try:
        candidates = await _find_backups(client)
    except Exception as exc:
        print(f"Не удалось найти резервную копию: {type(exc).__name__}: {exc}")
        return False
    if not candidates:
        print("Резервной копии в «Избранном» нет — старт с чистой базы.")
        return False
    for message in candidates:
        workdir = tempfile.mkdtemp(prefix="wm-restore-")
        try:
            gz = os.path.join(workdir, "backup.db.gz")
            await client.download_media(message, file=gz)
            if await asyncio.to_thread(_install, gz, path):
                Storage(path).reload()
                _state["mtime"] = os.stat(path).st_mtime_ns
                print(f"База восстановлена из резервной копии от {message.date:%Y-%m-%d %H:%M} UTC.")
                return True
            print("Резервная копия повреждена, пробую предыдущую…")
        except Exception as exc:
            print(f"Не удалось восстановить из копии: {type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    return False


async def backup_now(client, path: str, force: bool = False) -> bool:
    async with _lock:
        if not os.path.exists(path):
            return False
        mtime = os.stat(path).st_mtime_ns
        if not force and mtime == _state["mtime"]:
            return False  # nothing changed since the last upload
        workdir = tempfile.mkdtemp(prefix="wm-backup-")
        try:
            gz = await asyncio.to_thread(_snapshot_gz, path, workdir)
            size = os.path.getsize(gz)
            if size > settings.backup_max_mb * 1024 * 1024:
                print(f"Резервная копия {size // 1048576} МБ больше лимита BACKUP_MAX_MB={settings.backup_max_mb} — пропускаю.")
                _state["mtime"] = mtime
                return False
            await client.send_file("me", gz, caption=f"{TAG} {datetime.now(timezone.utc).isoformat()}",
                                   force_document=True)
            _state["mtime"] = mtime
            _state["size"] = size
            try:
                for old in (await _find_backups(client))[KEEP:]:
                    await old.delete()
            except Exception as exc:
                print(f"Не удалось удалить старые копии: {type(exc).__name__}: {exc}")
            print(f"Резервная копия базы отправлена в «Избранное» ({os.path.getsize(gz) // 1024} КБ).")
            return True
        except Exception as exc:
            print(f"Не удалось отправить резервную копию: {type(exc).__name__}: {exc}")
            return False
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


async def backup_loop(client, path: str):
    _state["client"] = client
    await asyncio.sleep(120)
    while True:
        try:
            # a big database is uploaded less often (every upload is the whole file)
            factor = 4 if _state.get("size", 0) > 40 * 1048576 else 1
            await asyncio.wait_for(WAKE.wait(), timeout=max(1, settings.backup_interval_min) * 60 * factor)
            await asyncio.sleep(15)  # collect a burst of changes into one upload
        except asyncio.TimeoutError:
            pass
        WAKE.clear()
        await backup_now(client, path)


async def final_backup(path: str, timeout: float = 20.0):
    """Called on SIGTERM (redeploy / spin-down). Best effort, bounded in time."""
    client = _state["client"]
    if not client or not settings.backup_enabled:
        return
    try:
        await asyncio.wait_for(backup_now(client, path), timeout=timeout)
    except Exception as exc:
        print(f"Финальная резервная копия не удалась: {type(exc).__name__}")
"""Evidence stores: the live one in data/, and archived ones in data/archives/<id>/.

Nothing is deleted. "Start a new store" first archives the live one whole (evidence, run logs and the saved run states that
hold each run's report, the search ledger, the archived pages and uploads), checks the archive (row counts, database
integrity, and a full audit that re-hashes every page and re-verifies every quote), and only then empties the live store.
If any check fails, the archive is undone and the live store is left as it was. Archived stores are read-only: their
files are marked read-only and they are opened in read-only mode, through the same pages as the live store
(/archives/<id> in the app). What stays live: connectors and their approvals, usage budgets, settings, the API token.

    uv run python -m stores list
    uv run python -m stores archive --label "before ontologies" --yes      (stop the server first, or use the app)
    uv run python -m stores verify 20261107-101500-before-ontologies
"""

import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone  # noqa: F401  (timedelta: tests)
from pathlib import Path

import config
from evidence import EvidenceStore
from runs import RunStore

ID_RE = re.compile(r"^\d{8}-\d{6}(?:-[a-z0-9-]{1,40})?$")
ACTIVE = {"queued", "running"}
WAITING = {"awaiting_approval", "paused", "needs_capability", "needs_source"}
_lock = threading.Lock()
_held = threading.Event()        # set while this process is the one archiving (so its own preview is not blocked by itself)


class StoreError(Exception):
    """The store could not be archived; `problems` says why. The live store is unchanged."""
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Layout:
    """Where a store's parts live. The live store uses config's paths; tests use a temporary folder."""
    data_dir: Path
    evidence_db: Path

    @classmethod
    def live(cls) -> "Layout":
        return cls(Path(config.DATA_DIR), Path(config.EVIDENCE_DB_PATH))

    @property
    def databases(self) -> dict[str, Path]:
        return {"evidence": self.evidence_db, "runs": self.data_dir / "runs.db",
                "checkpoints": self.data_dir / "checkpoints.db", "searches": self.data_dir / "search_ledger.db"}

    @property
    def folders(self) -> dict[str, Path]:
        return {"files": self.data_dir / "archive", "uploads": self.data_dir / "uploads"}

    @property
    def archives(self) -> Path:
        return self.data_dir / "archives"

    @property
    def log(self) -> Path:
        return self.data_dir / "maintenance.jsonl"


def busy() -> bool:
    """True while a store is being archived: new runs must wait."""
    return _lock.locked()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")[:40].strip("-")


def _counts(path: Path, readonly: bool = False) -> dict[str, int]:
    if not path.exists():
        return {}
    uri = f"file:{path.resolve().as_posix()}{'?mode=ro' if readonly else ''}"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


def _folder_size(path: Path) -> tuple[int, int]:
    files = [p for p in path.rglob("*") if p.is_file()] if path.exists() else []
    return len(files), sum(p.stat().st_size for p in files)


def _problem_keys(problems: list[str]) -> set[str]:
    """What an audit problem is about ('source 1a2b3c...: raw archive file is missing' -> 'source 1a2b3c: raw archive'), so
    the audit of the archive can be compared with the audit of the live store although the file paths differ."""
    out = set()
    for p in problems:
        head, _, rest = p.partition(": ")
        out.add(f"{head}: {rest.split(' file')[0].split(':')[0]}")
    return out


def _run_list(runs_db: Path, readonly: bool = False) -> list[dict]:
    if not runs_db.exists():
        return []
    return RunStore(str(runs_db), readonly=readonly).runs(100_000)


def preview(layout: Layout | None = None, extra_blockers: list[str] | tuple = ()) -> dict:
    """What archiving the live store would take away, and anything that stops it."""
    layout = layout or Layout.live()
    runs = _run_list(layout.databases["runs"])
    files = {k: dict(zip(("files", "bytes"), _folder_size(p))) for k, p in layout.folders.items()}
    active, abandoned = _active_runs(layout, runs)
    return {"counts": {k: _counts(p) for k, p in layout.databases.items()}, "folders": files,
            "runs": len(runs), "waiting": [{k: r[k] for k in ("run_id", "question", "status")} for r in runs if r["status"] in WAITING],
            "abandoned": abandoned,
            "blockers": [f"run {r['run_id'][:8]} is {r['status']} ({(r['question'] or 'no question').strip()[:80]}); wait for it to finish" for r in active]
                        + list(extra_blockers) + (["a store is being archived right now"] if busy() and not _held.is_set() else []),
            "since": _live_since(layout, runs)}


STALE_AFTER_MINUTES = 30      # a queued or running run that has written nothing for this long was cut off (e.g. a restart)


def _active_runs(layout: Layout, runs: list[dict]) -> tuple[list[dict], list[dict]]:
    """(runs still working, runs marked working that stopped long ago). A run writes an event every few seconds while it
    works; one silent for STALE_AFTER_MINUTES lost its process and will never finish."""
    store = RunStore(str(layout.databases["runs"])) if layout.databases["runs"].exists() else None
    active, abandoned = [], []
    for r in runs:
        if r["status"] not in ACTIVE:
            continue
        events = store.events(r["run_id"]) if store else []
        last = datetime.fromisoformat(events[-1]["ts"]) if events else None
        idle = (_now() - last).total_seconds() / 60 if last else None
        if idle is not None and idle > STALE_AFTER_MINUTES:
            abandoned.append({"run_id": r["run_id"], "question": (r["question"] or "").strip()[:120], "status": r["status"],
                              "last_activity": events[-1]["ts"]})
        else:
            active.append(r)
    return active, abandoned


def _close_abandoned(layout: Layout, abandoned: list[dict]) -> None:
    """Say, in the run's own log, that it was cut off: an event is added; nothing is changed."""
    store = RunStore(str(layout.databases["runs"]))
    for r in abandoned:
        store.emit(r["run_id"], "maintenance", "run_failed", {
            "error": f"abandoned: no activity since {r['last_activity'][:19]} (the server stopped while it worked); "
                     "closed when the store was archived"})


def _live_since(layout: Layout, runs: list[dict]) -> str | None:
    last = next((e for e in reversed(read_log(layout)) if e.get("action") == "archive"), None)
    if last:
        return last["at"]
    return min((r["started_at"] for r in runs), default=None)


def read_log(layout: Layout | None = None) -> list[dict]:
    layout = layout or Layout.live()
    if not layout.log.exists():
        return []
    return [json.loads(line) for line in layout.log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _log(layout: Layout, entry: dict) -> None:
    with layout.log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _copy_db(src: Path, dst: Path) -> None:
    """A consistent copy of a live database (WAL contents included), in plain rollback mode so it opens read-only."""
    with closing(sqlite3.connect(src, timeout=30)) as a, closing(sqlite3.connect(dst)) as b:
        a.backup(b)
        b.execute("PRAGMA journal_mode=DELETE")
        bad = b.execute("PRAGMA integrity_check").fetchone()[0]
    if bad != "ok":
        raise StoreError([f"{src.name}: integrity check of the copy failed: {bad}"])


def _read_only(folder: Path) -> None:
    for p in folder.rglob("*"):
        if p.is_file():
            os.chmod(p, stat.S_IREAD)


def _empty(path: Path) -> None:
    """Empty a live database in place, keeping its tables, so open connections stay valid. Its append-only triggers are
    dropped for this and put back by the store's own start-up code."""
    with closing(sqlite3.connect(path, timeout=30)) as db:
        triggers = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        db.execute("PRAGMA foreign_keys=OFF")
        for t in triggers:
            db.execute(f'DROP TRIGGER "{t}"')
        for t in tables:
            db.execute(f'DELETE FROM "{t}"')
        if db.execute("SELECT name FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
            db.execute("DELETE FROM sqlite_sequence")
        db.commit()
        try:
            db.execute("VACUUM")
        except sqlite3.OperationalError:
            pass                                  # another connection is reading; the space is reused anyway


def archive(label: str = "", reason: str = "", by: str = "app", layout: Layout | None = None, git_commit: str | None = None,
            extra_blockers: list[str] | tuple = ()) -> dict:
    """Archive the live store whole, check the archive, then empty the live store. Returns the archive's manifest."""
    layout = layout or Layout.live()
    if not _lock.acquire(blocking=False):
        raise StoreError(["a store is being archived right now"])
    try:
        _held.set()
        before = preview(layout, extra_blockers)
        if before["blockers"]:
            raise StoreError(before["blockers"])
        closed = before["abandoned"]
        if closed:
            _close_abandoned(layout, closed)
            before = preview(layout, extra_blockers)     # counts now include those closing events
        stamp = _now()
        aid = stamp.strftime("%Y%m%d-%H%M%S") + (f"-{_slug(label)}" if _slug(label) else "")
        final = layout.archives / aid
        work = layout.archives / f".{aid}.partial"
        if final.exists() or work.exists():
            raise StoreError([f"an archive called {aid} exists already"])
        work.mkdir(parents=True)
        moved: list[tuple[Path, Path]] = []
        try:
            live_problems = EvidenceStore(layout.evidence_db).audit() if layout.evidence_db.exists() else []
            for name, src in layout.databases.items():
                if src.exists():
                    _copy_db(src, work / f"{name}.db")
            for name, src in layout.folders.items():
                if src.exists():
                    os.replace(src, work / name)          # same disk: a rename, nothing is copied
                    moved.append((work / name, src))
                    src.mkdir()
            copied = {name: _counts(work / f"{name}.db", readonly=True) for name in layout.databases if (work / f"{name}.db").exists()}
            problems = [f"{name}.{t}: {before['counts'][name].get(t)} rows live, {n} in the archive"
                        for name, tables in copied.items() for t, n in tables.items() if before["counts"][name].get(t) != n]
            archived = EvidenceStore(work / "evidence.db", files_dir=work / "files", readonly=True) if (work / "evidence.db").exists() else None
            archive_problems = archived.audit() if archived else []
            if new := _problem_keys(archive_problems) - _problem_keys(live_problems):
                problems += [f"audit of the archive found problems the live store did not have: {sorted(new)[:5]}"]
            if problems:
                raise StoreError(problems)
        except Exception:
            for there, back in moved:                     # undo: the live store is exactly as it was
                if back.exists() and not any(back.iterdir()):
                    back.rmdir()
                os.replace(there, back)
            shutil.rmtree(work, ignore_errors=True)
            raise
        runs = _run_list(work / "runs.db", readonly=True)
        manifest = {
            "id": aid, "label": label.strip(), "reason": reason.strip(), "archived_at": stamp.isoformat(timespec="seconds"), "archived_by": by,
            "titan_commit": git_commit, "live_since": before["since"],
            "counts": copied, "folders": {k: dict(zip(("files", "bytes"), _folder_size(work / k))) for k in layout.folders if (work / k).exists()},
            "runs": len(runs), "unfinished_runs": before["waiting"], "abandoned_runs_closed": closed,
            "audit": {"problems": len(archive_problems), "same_as_live": True, "examples": archive_problems[:10]},
            "contains": ["evidence.db: sources, claims, rejected claims, checks, dates", "runs.db: every run's step-by-step log",
                         "checkpoints.db: saved run states, which hold each run's report", "searches.db: every search made",
                         "files/: the archived pages and their extracted text, named by hash", "uploads/: uploaded files"],
        }
        (work / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        _read_only(work)
        os.replace(work, final)
        for path in layout.databases.values():            # the archive is complete and checked: now start the new store
            if path.exists():
                _empty(path)
        EvidenceStore(layout.evidence_db)
        RunStore(str(layout.databases["runs"]))
        _log(layout, {"at": _now().isoformat(timespec="seconds"), "action": "archive", "id": aid, "label": manifest["label"],
                      "reason": manifest["reason"], "by": by, "runs": len(runs), "claims": copied.get("evidence", {}).get("claims", 0)})
        return manifest
    finally:
        _held.clear()
        _lock.release()


def list_archives(layout: Layout | None = None) -> list[dict]:
    layout = layout or Layout.live()
    if not layout.archives.exists():
        return []
    out = []
    for d in layout.archives.iterdir():
        if d.is_dir() and ID_RE.match(d.name) and (d / "manifest.json").exists():
            out.append(json.loads((d / "manifest.json").read_text(encoding="utf-8")))
    return sorted(out, key=lambda m: m["archived_at"], reverse=True)


class ArchivedStore:
    """One archived store, read-only: its evidence, its run logs and the reports saved with its runs."""

    def __init__(self, aid: str, layout: Layout | None = None):
        layout = layout or Layout.live()
        if not ID_RE.match(aid or "") or not (layout.archives / aid / "manifest.json").exists():
            raise KeyError(aid)
        self.id, self.dir = aid, layout.archives / aid
        self.manifest = json.loads((self.dir / "manifest.json").read_text(encoding="utf-8"))
        self.evidence = EvidenceStore(self.dir / "evidence.db", files_dir=self.dir / "files", readonly=True)
        self.runs = RunStore(str(self.dir / "runs.db"), readonly=True)

    def report(self, run_id: str) -> dict | None:
        """The report saved with a finished run, read from the archived run states without changing them."""
        path = self.dir / "checkpoints.db"
        if not path.exists():
            return None
        from langgraph.checkpoint.sqlite import SqliteSaver
        with closing(sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, check_same_thread=False)) as conn:
            saver = SqliteSaver(conn)
            saver.is_setup = True                 # the tables exist; setting them up would write
            found = saver.get_tuple({"configurable": {"thread_id": run_id}})
        return (found.checkpoint.get("channel_values") or {}).get("report") if found else None

    def verify(self) -> dict:
        """Audit the archive again now: every page re-hashed, every quote re-verified."""
        problems = self.evidence.audit()
        recorded = self.manifest.get("audit", {}).get("problems", 0)
        return {"id": self.id, "problems": len(problems), "recorded_at_archive": recorded, "unchanged": len(problems) == recorded,
                "examples": problems[:10]}


def reload_definitions() -> list[str]:
    """Forget every definition file read into memory (ontologies, skills, guidance), so edits take effect without a
    restart. Returns the names of the caches cleared."""
    root = Path(config.PROJECT_ROOT, "src").resolve()
    cleared = []
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if not path or root not in Path(path).resolve().parents:
            continue
        for attr, value in list(vars(module).items()):
            if callable(getattr(value, "cache_clear", None)) and getattr(value, "__module__", None) == module.__name__:
                value.cache_clear()
                cleared.append(f"{name}.{attr}")
    return sorted(cleared)


def _commit() -> str | None:
    try:
        import subprocess
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=config.PROJECT_ROOT, capture_output=True, text=True,
                              timeout=5).stdout.strip() or None
    except Exception:
        return None


def main(argv=None) -> None:
    import argparse
    p = argparse.ArgumentParser(prog="python -m stores")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("archive")
    a.add_argument("--label", default="")
    a.add_argument("--reason", default="")
    a.add_argument("--yes", action="store_true", help="really archive the live store and start a new one")
    sub.add_parser("verify").add_argument("id")
    args = p.parse_args(argv)
    if args.cmd == "list":
        print(json.dumps({"live": preview(), "archives": list_archives()}, indent=1))
    elif args.cmd == "archive":
        if not args.yes:
            print(json.dumps(preview(), indent=1))
            raise SystemExit("this is what would be archived; add --yes to archive it and start a new store")
        try:
            m = archive(args.label, args.reason, by="cli", git_commit=_commit())
        except StoreError as exc:
            raise SystemExit("not archived: " + "; ".join(exc.problems))
        print(f"archived as {m['id']}: {m['runs']} runs, {m['counts'].get('evidence', {}).get('claims', 0)} claims")
    else:
        print(json.dumps(ArchivedStore(args.id).verify(), indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])

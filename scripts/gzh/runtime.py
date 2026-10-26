"""Persistent single-owner runner with observable waits and bounded recovery."""
import fcntl
import json
import os
import time
from pathlib import Path
from collections import Counter
from .crawl import crawl, CrawlState, backfill_missing
from .exporter import KnowledgeBase, rebuild_index_from_files, write_manifest


def write_status(directory, **fields):
    path = Path(directory) / '.gzh-status.json'
    previous = json.loads(path.read_text()) if path.exists() else {}
    previous.update(fields, pid=os.getpid(), heartbeat=time.time())
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(previous, ensure_ascii=False, indent=2))
    temporary.replace(path)


def inspect_archive(root, account):
    kb = KnowledgeBase(root, account=account)
    state = CrawlState.load(kb.dir / '.weread-crawl.json')
    arts = rebuild_index_from_files(kb)
    urls = {a.url for a in arts}
    unresolved = [] if state is None else [v for key, v in state.seen.items() if v.get('resolved_url', key) not in urls]
    import re
    missing_images, remote_images = [], []
    for article in arts:
        path = kb.root / article.path
        if not path.exists():
            path = kb.dir / article.path
        for reference in re.findall(r'!\[[^\]]*\]\(([^)]+)\)', path.read_text()):
            if reference.startswith(('http://', 'https://')):
                remote_images.append({'article': article.path, 'image': reference})
            elif not (path.parent / reference).exists():
                missing_images.append({'article': article.path, 'image': reference})
    listed = len(state.seen) if state else None
    index_path = kb.dir / '.gzh-index.json'
    manifest_path = kb.dir / 'manifest.json'
    stored_index_count = json.loads(index_path.read_text()).get('count') if index_path.exists() else None
    manifest_count = json.loads(manifest_path.read_text()).get('count') if manifest_path.exists() else None
    table_path = kb.dir / 'index.md'
    table_count = len(re.findall(r'^\| \d{4}-', table_path.read_text(), re.M)) if table_path.exists() else None
    report = dict(list_finished=bool(state and state.finished), listed=listed,
                  total_known=bool(state and state.finished), files=len(arts),
                  archived_from_list=(listed - len(unresolved)) if listed is not None else None,
                  unresolved=unresolved, missing_images=missing_images, remote_images=remote_images,
                  months=dict(sorted(Counter(a.publish_date[:7] for a in arts).items())))
    report['counts'] = dict(files=len(arts), index=stored_index_count, manifest=manifest_count, table=table_count)
    report['counts_match'] = all(value == len(arts) for value in report['counts'].values())
    report['complete'] = bool(report['list_finished'] and report['counts_match'] and not unresolved and not missing_images and not remote_images)
    status = kb.dir / '.gzh-status.json'
    report['runtime'] = json.loads(status.read_text()) if status.exists() else {}
    heartbeat_path = kb.dir / '.gzh-heartbeat'
    if heartbeat_path.exists():
        report['runtime']['heartbeat'] = heartbeat_path.stat().st_mtime
    pid = report['runtime'].get('pid')
    try:
        if not pid: raise ProcessLookupError()
        os.kill(pid, 0)
        report['process_alive'] = True
    except (ProcessLookupError, PermissionError):
        report['process_alive'] = False
    return report


def wait_until(directory, deadline, reason):
    while True:
        remaining = deadline - time.time()
        write_status(directory, status='waiting', reason=reason, next_retry_at=deadline)
        if remaining <= 0: return
        time.sleep(min(remaining, 30))


def run(book_id, account, root, config=None):
    directory = Path(root).expanduser().resolve() / account
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / '.gzh-run.lock', 'a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('该公众号已有归档任务运行')
        import threading
        stop_heartbeat = threading.Event()
        def heartbeat():
            while not stop_heartbeat.wait(20):
                # Only the main thread mutates status; heartbeat touches its timestamp.
                path = directory / '.gzh-heartbeat'
                path.touch()
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        failures = 0
        try:
            while True:
                write_status(directory, status='running', reason='读取列表与保存正文', next_retry_at=None)
                try:
                    result = crawl(book_id, account, str(Path(root).expanduser().resolve()),
                                   config={**(config or {}), 'pages_per_run': 1, 'max_cooldowns_per_run': 0})
                except Exception as exc:
                    failures += 1
                    delay = min(900 * 2 ** min(failures - 1, 3), 7200)
                    wait_until(directory, time.time() + delay, '异常：' + str(exc)[:200])
                    continue
                reason = result['stopped_reason']
                if reason.startswith('登录失效：'):
                    write_status(directory, status='needs_login', reason=reason)
                    return 2
                if result['finished']:
                    backfill_missing(account, root, concurrency=1)
                    report = inspect_archive(root, account)
                    (directory / 'audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
                    kb = KnowledgeBase(root, account)
                    write_manifest(kb, rebuild_index_from_files(kb), '归档验收', extra=report)
                    write_status(directory, status='complete' if report['complete'] else 'needs_review', reason='查看 audit.json')
                    return 0 if report['complete'] else 1
                if any(marker in reason for marker in ('停滞', '重复', '空页后')):
                    write_status(directory, status='needs_review', reason=reason)
                    return 1
                if '页数用完' in reason:
                    failures = 0
                    continue
                from .ratelimit import check
                decision = check()
                failures += 1
                delay = max(decision.wait, min(900 * 2 ** min(failures-1, 3), 7200))
                # Date-based per-account budgets reset after local midnight.
                if '今日额度用尽' in reason:
                    import datetime
                    tomorrow = datetime.datetime.now().date() + datetime.timedelta(days=1)
                    delay = max(delay, datetime.datetime.combine(tomorrow, datetime.time()).timestamp() - time.time() + 1)
                wait_until(directory, time.time() + delay, reason)
        finally:
            stop_heartbeat.set()

"""Rate-limited reader content transport; no account cookies leave Chrome."""
import json
import subprocess
from pathlib import Path
from .fetcher import FetchError
from .ratelimit import acquire, note_error
from .content import parse_content


def fetch_reader_content(review_id):
    decision = acquire(label='content:' + review_id)
    if not decision.allowed:
        raise FetchError('额度或冷却限制：' + decision.reason)
    tool = Path(__file__).resolve().parent.parent / 'tools' / 'weread_content.mjs'
    proc = subprocess.run(['node', str(tool), review_id], capture_output=True, text=True, timeout=55)
    try:
        result = json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise FetchError('读取正文失败或超时') from exc
    if not result.get('ok'):
        raise FetchError(result.get('error', '读取正文失败'))
    raw = result['html']
    if raw.lstrip().startswith('{'):
        payload = json.loads(raw)
        code = payload.get('errCode')
        if code in (-2041, -2012):
            note_error('content:' + str(code))
        raise FetchError('微信读书正文返回错误：' + str(code))
    return parse_content(raw)

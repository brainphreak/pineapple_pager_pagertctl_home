"""duckyctl.py - runner for hak5-style payload.sh scripts.

Responsible for taking a payload directory (containing a `payload.sh`
plus a metadata header in `#` comments) and executing it under the
same environment the stock pineapplepager daemon would set up, so
that every duckyscript command in /usr/bin/ can reach /tmp/api.sock
and do its thing.

Usage:
    import duckyctl
    meta = duckyctl.parse_header('/mmc/root/payloads/user/_hello')
    runner = duckyctl.run_payload(
        '/mmc/root/payloads/user/_hello',
        on_line=lambda line: print(line),
    )
    runner.wait()        # block until finished
    print(runner.exit_code)

    # Or stop mid-run:
    runner.stop()
"""

import os
import re
import signal
import subprocess
import threading
import time


# Keys we recognize in the payload header comment block. Every key is
# a simple string; Description may wrap over multiple `#` lines without
# repeating the key.
_META_KEYS = ('Title', 'Author', 'Description', 'Category', 'Version')


def parse_header(payload_dir):
    """Parse the leading `#` comment block of <payload_dir>/payload.sh
    and return a dict of metadata.

    Returns:
        {'title': str, 'author': str, 'description': str,
         'category': str, 'version': str, 'path': str}
    Any missing field defaults to ''.
    """
    meta = {k.lower(): '' for k in _META_KEYS}
    meta['path'] = payload_dir
    sh = os.path.join(payload_dir, 'payload.sh')
    if not os.path.isfile(sh):
        return meta
    current_key = None
    try:
        with open(sh) as f:
            for line in f:
                stripped = line.rstrip('\n')
                # Stop at the first non-comment (non-shebang) line.
                if not stripped.lstrip().startswith('#'):
                    break
                # Drop leading '#' and a single optional space.
                body = stripped.lstrip()
                if body.startswith('#!'):
                    continue
                body = body[1:].lstrip()
                m = re.match(r'([A-Za-z][A-Za-z _\-]*?)\s*:\s*(.*)$', body)
                if m:
                    key = m.group(1).strip().lower().replace(' ', '_')
                    val = m.group(2).strip()
                    if key in meta:
                        meta[key] = val
                        current_key = key
                        continue
                    # Not a key we care about — if it's a long section header
                    # like `============`, stop accumulating into the prior key.
                    if re.match(r'^[=\-#*_~]{3,}$', body):
                        current_key = None
                    continue
                # Continuation line for the last-seen key (long descriptions).
                if current_key == 'description' and body:
                    sep = ' ' if meta['description'] else ''
                    meta['description'] += sep + body
    except Exception:
        pass
    return meta


class PayloadRunner:
    """Handle to a running payload subprocess."""

    def __init__(self, payload_dir, on_line=None):
        self.payload_dir = payload_dir
        self.on_line = on_line
        self.proc = None
        self.exit_code = None
        self.started_at = None
        self.stopped_at = None
        self._reader_thread = None
        self._lines = []              # full output buffer
        self._lock = threading.Lock()

    def start(self):
        """Launch the payload's entry script in a subprocess and
        begin streaming stdout/stderr line-by-line. Non-blocking.

        Entry script resolution — in priority order:
          1. `pagerctl.sh` in the payload dir — used by pagerctl-native
             payloads that know they're running under pagerctl_home and
             don't need the duckyscript API shim. They talk to the pager
             hardware directly and do NOT start/stop pineapplepager.
          2. `payload.sh` — the standard hak5 duckyscript entry point.
        """
        sh = os.path.join(self.payload_dir, 'pagerctl.sh')
        if not os.path.isfile(sh):
            sh = os.path.join(self.payload_dir, 'payload.sh')
        if not os.path.isfile(sh):
            raise FileNotFoundError(sh)
        env = self._build_env()
        self.started_at = time.time()
        self.proc = subprocess.Popen(
            ['/bin/bash', sh],
            cwd=self.payload_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            # New session so stop() can kill the whole tree.
            preexec_fn=os.setsid,
        )
        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _build_env(self):
        env = os.environ.copy()
        # Ensure /usr/bin is on PATH so duckyscript commands resolve.
        path = env.get('PATH', '')
        need_paths = ['/usr/sbin', '/usr/bin', '/sbin', '/bin']
        parts = path.split(':') if path else []
        for p in need_paths:
            if p not in parts:
                parts.insert(0, p)
        env['PATH'] = ':'.join(parts)
        # Metadata the payload may introspect.
        env['PAYLOAD_PATH'] = self.payload_dir
        env['PAYLOAD_NAME'] = os.path.basename(self.payload_dir)
        env['PAYLOAD_LOG'] = '/tmp/payload.log'
        # Some duckyscript commands read this for verbose curl output.
        env.setdefault('HAK5_API_VERBOSE', '0')
        return env

    def _read_loop(self):
        assert self.proc is not None
        try:
            for line in self.proc.stdout:
                line = line.rstrip('\n')
                with self._lock:
                    self._lines.append(line)
                if self.on_line:
                    try:
                        self.on_line(line)
                    except Exception:
                        pass
        except Exception:
            pass
        self.proc.wait()
        self.exit_code = self.proc.returncode
        self.stopped_at = time.time()

    def is_running(self):
        return self.proc is not None and self.exit_code is None

    def wait(self, timeout=None):
        if self.proc is None:
            return None
        self.proc.wait(timeout=timeout)
        # Join the reader thread so self.exit_code is populated before
        # the caller inspects it — there's a small race where proc.wait
        # returns before the stdout reader loop has flushed the last
        # line and recorded the return code.
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
        return self.exit_code

    def stop(self, timeout=3.0):
        """SIGTERM the whole process group, then SIGKILL if it doesn't
        exit within `timeout` seconds."""
        if self.proc is None or self.exit_code is not None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except Exception:
            try:
                self.proc.terminate()
            except Exception:
                pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            try:
                self.proc.wait(timeout=1.0)
            except Exception:
                pass

    def lines(self):
        with self._lock:
            return list(self._lines)


def run_payload(payload_dir, on_line=None):
    """Convenience helper: construct a PayloadRunner, start it, return
    the handle. Caller is responsible for wait() or stop()."""
    runner = PayloadRunner(payload_dir, on_line=on_line)
    runner.start()
    return runner


# ----------------------------------------------------------------------
# CLI: `python3 duckyctl.py run <payload_dir>` — useful for manual smoke
# testing from an SSH session. Prints every output line to stdout.
# ----------------------------------------------------------------------

if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3 or sys.argv[1] != 'run':
        print('usage: duckyctl.py run <payload_dir>')
        sys.exit(2)
    d = sys.argv[2]
    m = parse_header(d)
    print(f'[{m["title"] or os.path.basename(d)}] by {m["author"] or "?"}  v{m["version"] or "?"}')
    if m['description']:
        print(f'  {m["description"][:160]}')
    r = run_payload(d, on_line=lambda ln: print(ln))
    try:
        r.wait()
    except KeyboardInterrupt:
        print('\n[ interrupted — sending SIGTERM ]')
        r.stop()
    print(f'[ exit {r.exit_code} ]')

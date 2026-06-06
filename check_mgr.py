"""Node health check - concurrent TCP connectivity testing"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import socket
import time
import logging

log = logging.getLogger('check')

class CheckManager:
    def __init__(self, database):
        self.db = database
        self.timeout = 3
        self.max_workers = 30

    def check_one(self, idx, data):
        if idx < 0 or idx >= len(data):
            return {'ok': False, 'error': 'invalid index'}
        item = data[idx]
        ip = item.get('ip', '')
        port = item.get('port', '')
        try:
            port_int = int(port)
        except:
            return {'ok': False, 'error': 'invalid port'}
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        start = time.time()
        try:
            s.connect((ip, port_int))
            ms = round((time.time() - start) * 1000, 1)
            s.close()
            log.info(f"Check OK: {item.get('name','')} ({ip}:{port}) {ms}ms")
            return {'ok': True, 'ms': ms}
        except Exception as e:
            ms = round((time.time() - start) * 1000, 1)
            log.info(f"Check FAIL: {item.get('name','')} ({ip}:{port}) {ms}ms - {e}")
            return {'ok': False, 'ms': ms, 'error': str(e)}

    def check_all(self, data):
        results = {}

        def _check_one(idx, ip, port):
            try:
                port_int = int(port)
            except:
                return idx, {'ok': False, 'ms': 0, 'error': 'invalid port'}
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            start = time.time()
            try:
                s.connect((ip, port_int))
                ms = round((time.time() - start) * 1000, 1)
                s.close()
                return idx, {'ok': True, 'ms': ms}
            except Exception as e:
                ms = round((time.time() - start) * 1000, 1)
                return idx, {'ok': False, 'ms': ms, 'error': str(e)}

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(_check_one, i, item.get('ip', ''), item.get('port', '')): i
                for i, item in enumerate(data)
            }
            for fut in as_completed(futures):
                idx, result = fut.result()
                results[str(idx)] = result

        ok_count = sum(1 for v in results.values() if v['ok'])
        log.info(f"Check all: {ok_count}/{len(data)} OK")
        return results

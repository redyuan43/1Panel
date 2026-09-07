import http.client
import importlib.util
import json
import pathlib
import unittest
from unittest import mock
ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('isolated_gateway_fixture',ROOT/'tests/test_qwen36_prefix_gateway.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)

class QueueTelemetryReview(unittest.TestCase):
    setUp=fixture.CacheTests.setUp
    def terminal(self, operation):
        import time
        deadline=time.monotonic()+1
        while time.monotonic()<deadline:
            value=self.cache.telemetry.get(operation) or {}
            if value.get('terminal'):return value
            time.sleep(.005)
        self.fail('gateway did not publish terminal telemetry')
    def test_gateway_generated_failure_advertises_available_telemetry(self):
        self.config['queue_timeout']=.01
        operation='0123456789abcdef0123456789abcdef'
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        try:
            with self.cache.lock:
                conn.request('POST','/v1/chat/completions',body=b'{"messages":[]}',headers={'Authorization':'Bearer cpu-test-secret','Content-Type':'application/json','X-Request-ID':'synthetic-request','X-1Panel-Attempt':'1','X-1Panel-Operation-ID':operation})
                response=conn.getresponse();response.read()
            value=self.terminal(operation)
            self.assertEqual(response.status,503)
            self.assertTrue(value['terminal'])
            self.assertEqual(self.state['calls'],[])
            print('queue_failure='+json.dumps({'status':response.status,'capability_header':response.getheader('X-Prefix-Telemetry'),'terminal_telemetry_exists':True,'backend_calls':0}))
            self.assertEqual(response.getheader('X-Prefix-Telemetry'),'1','Router collector starts only when this gateway capability header is present')
        finally:conn.close()

    def test_preparation_failure_advertises_telemetry_and_releases_lock(self):
        operation='1123456789abcdef0123456789abcdef'
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        try:
            with mock.patch.object(self.cache, 'prepare', side_effect=RuntimeError('synthetic preparation failure')):
                conn.request('POST','/v1/chat/completions',body=b'{"messages":[]}',headers={'Authorization':'Bearer cpu-test-secret','Content-Type':'application/json','X-Request-ID':'synthetic-request','X-1Panel-Attempt':'1','X-1Panel-Operation-ID':operation})
                response=conn.getresponse();response.read()
            self.assertEqual(response.status,502)
            self.assertEqual(response.getheader('X-Prefix-Telemetry'),'1')
            # HTTP completion may precede the handler's final telemetry publish.
            self.assertTrue(self.terminal(operation)['terminal'])
            self.assertTrue(self.cache.lock.acquire(timeout=1))
            self.cache.lock.release()
            self.assertEqual(self.state['calls'],[])
        finally:conn.close()

    def test_unauthenticated_request_does_not_advertise_registered_operation(self):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        try:
            conn.request('POST','/v1/chat/completions',body=b'{}')
            response=conn.getresponse();response.read()
            self.assertEqual(response.status,401)
            self.assertIsNone(response.getheader('X-Prefix-Telemetry'))
        finally:conn.close()

if __name__=='__main__':unittest.main(verbosity=2)

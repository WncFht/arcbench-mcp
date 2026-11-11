"""Submission chain and TLS fallback safety, without paid model calls."""
import asyncio
import json
import subprocess
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest

from arcbench_mcp.api import ArcBenchClient, ArcBenchError
from arcbench_mcp.transport import CompatibleTransport, CurlTransport


@pytest.fixture
def bundle(tmp_path):
    path = tmp_path / 'agent.zip'
    with ZipFile(path, 'w') as z:
        z.writestr('main.py', 'print("ok")')
        z.writestr('requirements.txt', '')
    return path


def mock_client(handler):
    c = ArcBenchClient(cookie='session=test')
    c._http.close()
    c._http = httpx.Client(base_url='https://example.test/api', transport=httpx.MockTransport(handler))
    return c


def test_submission_upload_create_start(bundle):
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.path == '/api/submissions':
            body = request.read()
            assert b'name="api_key"' in body and b'test-secret' in body
            assert b'name="competition_id"' in body and b'smoke' in body
            assert b'filename="agent.zip"' in body
            return httpx.Response(200, json={'submission': {'id': 's1'}})
        if request.url.path == '/api/runs':
            assert request.read() == b'submission_id=s1&requirement_id=task'
            return httpx.Response(200, json={'run': {'id': 'r1'}})
        assert request.url.path == '/api/runs/r1/start'
        return httpx.Response(200, json={'id': 'r1', 'status': 'QUEUED'})
    result = mock_client(handler).submit(bundle, 'task', competition_id='smoke', base_url='https://models.test/v1', api_key='test-secret')
    assert len(seen) == 3 and result['run_id'] == 'r1'
    assert 'test-secret' not in json.dumps(result)


@pytest.mark.parametrize('failed_stage', ['create_run', 'start_run'])
def test_partial_failure_preserves_ids_and_does_not_retry(bundle, failed_stage):
    seen = []
    def handler(request):
        seen.append(request.url.path)
        if len(seen) == 1:
            return httpx.Response(200, json={'id': 's1'})
        if len(seen) == 2 and failed_stage == 'start_run':
            return httpx.Response(200, json={'id': 'r1'})
        return httpx.Response(500, json={'detail': 'bad test-secret'})
    with pytest.raises(ArcBenchError) as exc:
        mock_client(handler).submit(bundle, 'task', base_url='https://models.test/v1', api_key='test-secret')
    message = str(exc.value)
    assert failed_stage in message and 'submission_id=s1' in message
    assert ('run_id=r1' in message) == (failed_stage == 'start_run')
    assert 'test-secret' not in message
    assert len(seen) == (3 if failed_stage == 'start_run' else 2)


def test_invalid_zip_creates_nothing(tmp_path):
    path = tmp_path / 'bad.zip'
    path.write_bytes(b'not zip')
    c = mock_client(lambda request: pytest.fail('must not send invalid upload'))
    with pytest.raises(ArcBenchError, match='有效 zip'):
        c.submit(path, 'task', base_url='https://models.test/v1', api_key='x')


def test_fallback_only_for_connect_error():
    t = CompatibleTransport()
    seen = []
    def primary(request):
        seen.append('primary')
        raise httpx.ConnectError('TLS setup failed')
    def fallback(request):
        seen.append('fallback')
        assert request.read() == b'payload'
        return httpx.Response(200, json={'ok': True})
    t.primary.close()
    t.primary = httpx.MockTransport(primary)
    t.fallback = httpx.MockTransport(fallback)
    r = httpx.Client(transport=t).post('https://example.test', content=b'payload')
    assert r.json() == {'ok': True} and seen == ['primary', 'fallback']


@pytest.mark.parametrize('error', [httpx.ReadTimeout, httpx.ReadError, httpx.WriteError])
def test_ambiguous_write_is_never_replayed(error):
    t = CompatibleTransport()
    def primary(request):
        raise error('response lost')
    t.primary.close()
    t.primary = httpx.MockTransport(primary)
    t.fallback = httpx.MockTransport(lambda r: pytest.fail('write must not be replayed'))
    with pytest.raises(error):
        httpx.Client(transport=t).post('https://example.test', content=b'payload')


def test_curl_keeps_secrets_out_of_argv_and_preserves_multipart(monkeypatch):
    def run(args, **kwargs):
        assert 'secret-value' not in str(args)
        assert '--insecure' not in args and '--retry' not in args
        header_path = Path(args[args.index('--header') + 1][1:])
        assert 'cookie: secret-value' in header_path.read_text().lower()
        assert header_path.stat().st_mode & 0o777 == 0o600
        assert b'upload bytes' in kwargs['input']
        Path(args[args.index('--dump-header') + 1]).write_text('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n')
        Path(args[args.index('--output') + 1]).write_bytes(b'{"ok":true}')
        return subprocess.CompletedProcess(args, 0, b'200', b'')
    monkeypatch.setattr('arcbench_mcp.transport.shutil.which', lambda x: '/usr/bin/curl')
    monkeypatch.setattr('arcbench_mcp.transport.subprocess.run', run)
    c = httpx.Client(transport=CurlTransport(), headers={'Cookie': 'secret-value'})
    assert c.post('https://example.test', files={'file': ('agent.zip', b'upload bytes')}).json()['ok']


def test_submit_is_exposed_as_write_tool():
    from arcbench_mcp.server import mcp
    tools = asyncio.run(mcp.list_tools())
    submit = next(t for t in tools if t.name == 'arc_submit')
    assert submit.annotations.readOnlyHint is False
    assert submit.annotations.idempotentHint is False
    assert 'api_key' in submit.inputSchema['required']

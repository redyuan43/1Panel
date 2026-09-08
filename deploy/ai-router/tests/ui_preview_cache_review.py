"""Browser-only synthetic cache review fixture; never accesses live state."""
import argparse
import asyncio
import json
from pathlib import Path
import uvicorn
from fastapi.responses import JSONResponse
from ui_preview import prepare
from ai_router.control import create_app

async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=14808)
    args=parser.parse_args()
    runtime=await prepare(args.state_dir.resolve())
    app=create_app(runtime)
    evidence={'isolated': True, 'model_calls': 'disabled by rejecting MockTransport', 'actions': [], 'conflict_injected': False}

    @app.middleware('http')
    async def isolate_actions(request, call_next):
        allowed=request.method == 'POST' and request.url.path.startswith('/api/endpoints/edge-qwen38-flash/actions/')
        if request.method not in {'GET','HEAD','OPTIONS'} and not allowed:
            return JSONResponse({'error': {'code': 'preview_read_only', 'message': 'Preview management action disabled'}}, status_code=403)
        record=None
        if allowed:
            body=await request.json()
            record={'path': request.url.path, 'expected_revision': body.get('expected_revision')}
            if request.headers.get('authorization') == 'Bearer ui-preview-only' and not evidence['conflict_injected']:
                revision=await runtime.endpoint_configs.revision()
                await runtime.endpoint_configs._set_revision(revision+1)
                evidence['conflict_injected']=True
            record['current_revision']=await runtime.endpoint_configs.revision()
            evidence['actions'].append(record)
        response=await call_next(request)
        if record is not None:
            record['status']=response.status_code
            (args.state_dir/'browser-action-evidence.json').write_text(json.dumps(evidence, indent=2))
        return response

    @app.get('/__ui_preview__')
    async def marker():
        return evidence

    server=uvicorn.Server(uvicorn.Config(app, host='127.0.0.1',port=args.port,log_level='warning',access_log=False))
    try:
        await server.serve()
    finally:
        await runtime.close()

if __name__=='__main__':
    asyncio.run(main())

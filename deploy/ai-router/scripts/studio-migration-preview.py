"""Ivan migration preview: shared UI, canonical AI business API, no local task state."""
import argparse
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask


def build(static: Path, upstream: str):
    parsed=urlparse(upstream)
    if parsed.scheme != 'https' or not parsed.hostname.endswith('.ts.net') or parsed.username or parsed.password or parsed.path not in {'','/'}:
        raise ValueError('Preview upstream must be the trusted HTTPS Tailscale control endpoint')
    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(base_url=upstream.rstrip('/'),timeout=httpx.Timeout(60,connect=10),trust_env=False) as client:
            app.state.client=client
            yield
    app=FastAPI(lifespan=lifespan)
    app.mount('/assets',StaticFiles(directory=static))
    @app.get('/media')
    @app.get('/')
    async def page():
        return FileResponse(static/'studio.html',headers={'Cache-Control':'no-store'})
    @app.get('/health')
    async def health():
        return {'role':'migration_preview','state_owner':'ai','gpu_owner':'ivan'}
    @app.api_route('/api/media/{path:path}',methods=['GET','POST','DELETE'])
    async def proxy(path: str, request: Request):
        if any(part in {'.','..'} for part in path.split('/')):
            return JSONResponse({'error':{'message':'Invalid path'}},status_code=400)
        content=bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content)>55*1024*1024:
                return JSONResponse({'error':{'message':'素材过大'}},status_code=413)
        headers={k:v for k,v in request.headers.items() if k.lower() in {'authorization','content-type','idempotency-key','last-event-id','range','if-range'}}
        upstream_request=request.app.state.client.build_request(request.method,'/api/media/'+path,params=request.query_params,headers=headers,content=bytes(content))
        response=await request.app.state.client.send(upstream_request,stream=True)
        headers={k:v for k,v in response.headers.items() if k.lower() in {'content-type','content-length','content-disposition','content-range','accept-ranges','cache-control','x-request-id'}}
        return StreamingResponse(response.aiter_raw(),status_code=response.status_code,headers=headers,background=BackgroundTask(response.aclose))
    return app


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--static',type=Path,required=True)
    parser.add_argument('--upstream',required=True)
    parser.add_argument('--port',type=int,default=14827)
    args=parser.parse_args()
    uvicorn.run(build(args.static.resolve(),args.upstream),host='127.0.0.1',port=args.port,access_log=False)

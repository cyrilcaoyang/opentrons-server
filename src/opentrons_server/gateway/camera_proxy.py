"""Camera-only forwarding to a separately owned service; no OT-2 I/O."""
import json
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask


def camera_router(config_path, require_identity):
    config=json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    router=APIRouter(prefix='/cameras',tags=['cameras'],dependencies=[Depends(require_identity)])
    aliases=config['cameras']
    allowed={('GET','status'),('GET','snapshot.jpg'),('GET','stream.mjpg')}

    @router.get('')
    async def cameras():
        return {'cameras':[{'id':alias,'kind':'usb','capabilities':['color','snapshot','mjpeg'],
                'urls':{k:f'/cameras/{alias}/{v}' for k,v in
                        {'status':'status','snapshot':'snapshot.jpg','stream':'stream.mjpg'}.items()}}
                for alias in aliases]}

    @router.get('/{alias}/{operation}')
    async def forward(alias: str,operation: str,request: Request):
        if alias not in aliases or ('GET',operation) not in allowed:
            raise HTTPException(404,'camera_route_not_found')
        client=httpx.AsyncClient(base_url=config['url'],trust_env=False,timeout=40,
                    headers={'Authorization':'Bearer '+config['token']})
        path=f'/v1/cameras/{quote(aliases[alias],safe="")}/{operation}'
        try:
            if operation=='stream.mjpg':
                r=await client.send(client.build_request('GET',path,params=request.query_params),stream=True)
                if r.status_code!=200:
                    data=await r.aread()
                    await r.aclose()
                    await client.aclose()
                    return Response(data,status_code=r.status_code,media_type='application/json')
                async def cleanup():
                    await r.aclose()
                    await client.aclose()
                return StreamingResponse(r.aiter_bytes(),media_type=r.headers.get('content-type'),
                    headers={'Cache-Control':'no-store'},background=BackgroundTask(cleanup))
            r=await client.get(path,params=request.query_params)
            return Response(r.content,status_code=r.status_code,media_type=r.headers.get('content-type'),
                headers={k:v for k,v in r.headers.items() if k.lower() in ('x-frame-number','x-frame-timestamp-ms','cache-control')})
        except httpx.HTTPError:
            await client.aclose()
            raise HTTPException(503,'camera_service_unavailable')
        finally:
            if operation!='stream.mjpg':
                await client.aclose()
    return router
